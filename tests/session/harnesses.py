"""A test's session harnesses ended before its cleanup removes what they write."""

import contextlib
import fcntl
import os
import shutil
import signal
import threading
import time

from ensemble.session.engine import hosted_session
from ensemble.session.engine.hosted_session import HARNESS, HostedSession
from ensemble.session.engine.types import ConfigError, Mode, SessionError
from ensemble.session.harness import process
from ensemble.session.harness.channel import HEX, SLICE, Channel, Tie
from ensemble.session.harness.dispatch import load_handlers
from ensemble.session.harness.types import Op

WAIT = 60  # s, past process.DRAIN
KILL_WAIT = 10  # s, the bound for a signalled harness to let go
ANSWER_WAIT = 10
_clock, _pause = time.monotonic, time.sleep  # the real ones, past a test's fake clock


def _let_go(channel: Channel, timeout: float) -> bool:
    try:
        fd = os.open(channel.lock, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return True
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(SLICE)
            else:
                return True
    finally:
        os.close(fd)


def end(home: str, wait: float = WAIT) -> None:
    """Close each live harness under home and wait for it to let go of its lock."""
    root = os.path.join(home, ".ensemble", "harness")
    try:
        names = sorted(os.listdir(root))
    except (FileNotFoundError, NotADirectoryError):
        return
    live = [
        c for c in (Channel(os.path.join(root, n)) for n in names if HEX.fullmatch(n)) if c.alive()
    ]
    deadline = time.monotonic() + wait
    for channel in live:
        with contextlib.suppress(SessionError):  # the wait below decides
            channel.request(Op.CLOSE, timeout=max(0.0, deadline - time.monotonic()))
    if stuck := [c for c in live if not _let_go(c, max(0.0, deadline - time.monotonic()))]:
        for channel in stuck:
            _signal(channel)
        running = [c.directory for c in stuck]
        raise AssertionError(
            f"session harnesses still running after {wait}s, then signalled: {running}"
        )


def _signallable(pid: object) -> bool:
    return type(pid) is int and pid > 1 and pid != os.getpid()


def _signal(channel: Channel) -> None:
    """SIGTERM, then SIGKILL, each only while its lock is held."""
    pid = channel.status().get("pid")
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not _signallable(pid) or not channel.alive():
            return
        try:
            os.kill(pid, sig)
        except (OSError, OverflowError):
            return
        if _let_go(channel, KILL_WAIT):
            return


def spawned(argv) -> bool:
    return HARNESS in argv


class Inline:
    """A spawned harness run here, so fakes answer: a failing start."""

    def __init__(self, argv) -> None:
        channel = Channel(argv[-1])
        channel.hold()
        try:
            self.returncode = process.Harness(channel).run(process._begin(channel.directory))
        finally:
            channel.release()
        self.pid = os.getpid()

    def poll(self) -> int:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def terminate(self) -> None:
        pass

    kill = terminate


def threaded_spawn(cls, spec, mode, *, display, host, idle_timeout):
    roots = cls._roots(spec.client)
    channel = Channel.create()
    channel.write_request(cls.harness_request(spec, display=display, host=host, roots=roots))
    tie = None if mode in (Mode.TUI, Mode.IN_PROCESS) else Tie()
    caller = None if tie is None else os.dup(tie.far)

    def begin(_request):
        modules = load_handlers(spec.handlers, roots, load=True)
        worker = cls.own(spec, display=display, host=host, owner=channel.directory)
        return worker, modules, [handler.state for handler in spec.handlers]

    def run():
        channel.hold()
        try:
            process.Harness(channel, caller=caller).run(begin)
        finally:
            channel.release()
            if caller is not None:
                with contextlib.suppress(OSError):
                    os.close(caller)

    thread = threading.Thread(target=run, daemon=True, name=f"harness {channel.directory}")
    thread.start()
    _RUNNING.append((channel, thread))
    try:
        status = hosted_session._await_start(
            channel, lambda: not thread.is_alive(), clock=_clock, pause=_pause
        )
    except (ConfigError, SessionError):
        thread.join(WAIT)
        if tie is not None:
            tie.close()
        shutil.rmtree(channel.directory, ignore_errors=True)
        raise
    if tie is not None:
        tie.handed()
    return cls._view(
        channel,
        name=spec.name,
        client=spec.client,
        sid=spec.sid,
        generation=status["generation"],
        host=host,
        idle_timeout=idle_timeout,
        mode=mode,
        spec=spec,
        tie=tie,
    )


class Threaded(HostedSession):
    """Injected callables run its harness on a thread (threaded_spawn), else in a process."""

    @classmethod
    def configure(cls, *, clients, **settings):
        table = clients.get("claude") if isinstance(clients, dict) else None
        if isinstance(table, dict) and "accounts" not in table:
            found = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
            clients = {**clients, "claude": {**table, "accounts": {"test": {"dir": found}}}}
        super().configure(clients=clients, **settings)

    @classmethod
    def _spawn(cls, spec, mode, **options):
        if hosted_session._reconstructible(cls._config()):
            return super()._spawn(spec, mode, **options)
        return threaded_spawn(cls, spec, mode, **options)


_RUNNING: list[tuple[Channel, threading.Thread]] = []


@contextlib.contextmanager
def joined():
    """Closes and joins the threaded harnesses still running on exit."""
    try:
        yield
    finally:
        running, _RUNNING[:] = list(_RUNNING), []
        for channel, thread in running:
            if thread.is_alive() and channel.alive():
                with contextlib.suppress(SessionError):
                    channel.request(Op.CLOSE, timeout=WAIT)
            thread.join(WAIT)


def until(check, wait: float = 5) -> None:
    deadline = _clock() + wait
    while not check():
        if _clock() > deadline:
            raise AssertionError(f"timed out after {wait} s")
        _pause(0.01)


def serve(case, channel: Channel, answer, *, count: int | None = 1) -> list[tuple[Op, dict]]:
    served: list[tuple[Op, dict]] = []
    done = threading.Event()

    def run():
        deadline = _clock() + ANSWER_WAIT
        while not done.is_set() and (count is None or _clock() < deadline):
            for ident, op, data in channel.pending():
                served.append((op, data))
                answer(ident, op, data)
                if len(served) == count:
                    return
            _pause(0.01)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    case.addCleanup(thread.join, ANSWER_WAIT)
    case.addCleanup(done.set)
    return served


class HeldOwner:
    def __init__(self, case, channel: Channel) -> None:
        self.case, self.channel, self.held = case, channel, None

    def hold(self, **status) -> None:
        if self.held is None:
            self.held = os.open(self.channel.lock, os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(self.held, fcntl.LOCK_EX)
            self.case.addCleanup(self.let_go)
        self.channel.write_status({"state": "running", **status})

    def let_go(self) -> None:
        if self.held is not None:
            os.close(self.held)
            self.held = None

    def serve(self, *replies: dict, before=None, until_cleanup=False) -> list[Op]:
        """A reply per request, letting go after an ok one; until_cleanup: replies[0] for each."""
        ops: list[Op] = []

        def answer(ident, op, _data):
            ops.append(op)
            if before is not None:
                before()
            reply = replies[0] if until_cleanup else replies[len(ops) - 1]
            self.channel.reply(ident, **reply)
            if reply.get("ok") and not until_cleanup:
                self.let_go()

        serve(self.case, self.channel, answer, count=None if until_cleanup else len(replies))
        return ops
