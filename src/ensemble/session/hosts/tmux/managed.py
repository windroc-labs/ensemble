"""Local guard channels. Only their creating harness holds the lifecycle pipe's write end."""

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable

from ensemble.session.engine.tmux_cli import PROCESS_ID, TmuxPane, TuiError
from ensemble.session.engine.types import (
    MARKS,
    ConfigError,
    Launch,
    Liveness,
    PaneStatus,
    SessionBusy,
    SessionError,
    StatusState,
    Unanswered,
)
from ensemble.session.harness import guard
from ensemble.session.hosts.base import GUARD_HOOK_SOCKET, GUARD_LEASE_FD, GUARD_WAIT
from ensemble.session.hosts.managed import ManagedProcess

GENERATION = re.compile(r"[A-Za-z0-9_-]{1,64}")
ONE_SHOT = re.compile(r"o[0-9a-f]{15}")
SWEEP_AGE = 60  # seconds a new one-shot dir may take to lock its owner.lock
LOG = logging.getLogger("session")


class TmuxManagedProcess(ManagedProcess):
    """Its display: the host tmux session of its name, while tagged with its generation; opener
    starts it (None: it has none); marked: its last status mark, kept for a reopened display.
    """

    def __init__(
        self,
        name,
        generation,
        proc,
        *,
        terminal,
        tmux,
        opener: Callable[[], None] | None = None,
        start_reader=True,
    ):
        self.tmux, self.opener = tmux, opener
        self.marked: StatusState | None = None
        self.removal: Callable[[], None] | None = None  # a one-shot dir's, after its close
        super().__init__(name, generation, proc, terminal=terminal, start_reader=start_reader)

    def _display(self) -> TmuxPane | None:
        """Its own generation's display pane, else None (headless, another's, gone, or tmux
        failed).
        """
        if not self.terminal:
            return None
        try:
            if self.tmux.option(self.name, "@generation") != self.generation:
                return None
            process = self.tmux.option(self.name, "@process")
        except TuiError:
            return None
        return (
            TmuxPane(self.name, process, self.tmux) if PROCESS_ID.fullmatch(process or "") else None
        )

    def activity(self) -> StatusState | None:
        pane = self._display()
        try:
            value = None if pane is None else pane.option("@state")
        except SessionError:
            return None
        return StatusState(value) if value in MARKS else None

    def mark(self, word: StatusState, events: str | None) -> None:
        self.marked = word
        pane = self._display()
        if pane is not None:
            with contextlib.suppress(SessionError):
                pane.mark(word, events)

    def close(self) -> None:
        try:
            super().close()
        finally:
            if self.proc.poll() is not None:  # its guard gone: nothing holds the dir any more
                removal, self.removal = self.removal, None
                if removal is not None:
                    removal()

    def close_display(self) -> None:
        if not self.terminal:
            return
        with contextlib.suppress(TuiError):
            if self.tmux.option(self.name, "@generation") == self.generation:
                self.tmux.kill(self.name)

    def redisplay(self) -> bool:
        """False while its own display runs; its own dead one is replaced. Another session of its
        name is refused, left alone; the worker is never restarted.
        """
        if self.opener is None:
            return False
        self.checked()
        if self.code is not None:
            raise SessionError(f"worker {self.name}: exited")
        try:
            shown = self.tmux.option(self.name, "@generation")
            if shown is not None:
                if shown != self.generation:
                    raise SessionError(f"worker {self.name}: its name shows another display")
                state = self.tmux.status(self.name)
                if state == PaneStatus.RUNNING:
                    return False
                if state == PaneStatus.UNKNOWN:
                    raise Unanswered(f"worker {self.name}: its display unknown")
                if state is not None:
                    self.tmux.kill(self.name)
            self.opener()
        except TuiError as error:
            raise SessionError(f"worker {self.name}: display: {error}") from error
        if self.marked is not None:
            self.mark(self.marked, None)  # its state again, no second event line
        return True


def private_directory(path: str) -> None:
    if not os.path.lexists(path):
        parent = os.path.dirname(path)
        if not os.path.isdir(parent):
            private_directory(parent)
        with contextlib.suppress(FileExistsError):
            os.mkdir(path, 0o700)
    check_directory(path)


def check_directory(path: str) -> None:
    info = os.lstat(path)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise OSError("guard directory is not private")


def control_root() -> str:
    root = os.environ.get("ENSEMBLE_CONTROL_ROOT", tempfile.gettempdir())
    return os.path.join(root, f"eg-{os.getuid()}")


def location(host: str, name: str) -> str:
    return os.path.join(control_root(), hashlib.sha256(f"{host}\0{name}".encode()).hexdigest()[:16])


def one_shot(host: str, generation: str) -> str:
    """A headless worker's guard dir: its generation's, never a tui's."""
    key = hashlib.sha256(f"{host}\0{generation}".encode()).hexdigest()[:15]
    return os.path.join(control_root(), f"o{key}")


def load_json(path: str) -> dict:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor) as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 4096
        ):
            raise OSError("invalid guard record")
        value = json.loads(stream.read(4097))
    if not isinstance(value, dict):
        raise ValueError("invalid guard record")
    return value


def save_json(path: str, value: dict) -> None:
    descriptor = os.open(
        path + ".new", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream)
    os.replace(path + ".new", path)


def lease_file(path: str, *, create: bool = True) -> int:
    descriptor = os.open(
        path, os.O_RDWR | (os.O_CREAT if create else 0) | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
    )
    info = os.fstat(descriptor)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        os.close(descriptor)
        raise OSError("invalid guard lease")
    return descriptor


def held(path: str) -> bool:
    """Whether another holds lock file `path`; absent: free."""
    try:
        descriptor = lease_file(path, create=False)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(descriptor)
    return False


def sweep(root: str) -> None:
    """Removes the one-shot dirs whose owner died before its close: owner lock and leases free."""
    if not os.path.lexists(root):
        return
    try:
        check_directory(root)
        entries = os.listdir(root)
    except OSError as error:
        LOG.warning("guard sweep %s: %s", root, error)
        return
    for entry in filter(ONE_SHOT.fullmatch, entries):
        directory = os.path.join(root, entry)
        try:
            check_directory(directory)
            if time.time() - os.lstat(directory).st_mtime < SWEEP_AGE:
                continue
            leases = [
                os.path.join(directory, run, "lease")
                for run in os.listdir(directory)
                if GENERATION.fullmatch(run)
            ]
            if not any(map(held, [os.path.join(directory, "owner.lock"), *leases])):
                shutil.rmtree(directory)
        except OSError as error:
            LOG.warning("guard sweep %s: %s", directory, error)


def liveness(host: str, name: str, generation: str) -> Liveness:
    directory = location(host, name)
    try:
        root = os.path.dirname(directory)
        if os.path.lexists(root):
            check_directory(root)
        if not os.path.lexists(directory):
            return Liveness.ABSENT
        check_directory(directory)
        if not GENERATION.fullmatch(generation):
            return Liveness.UNKNOWN
        current = load_json(os.path.join(directory, "current.json"))
        if current != {"generation": generation}:
            return Liveness.UNKNOWN
        run = os.path.join(directory, generation)
        check_directory(run)
        descriptor = lease_file(os.path.join(run, "lease"), create=False)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return Liveness.LIVE
            value = load_json(os.path.join(run, "state.json"))
            return (
                Liveness.ABSENT
                if value == {"generation": generation, "state": "exited"}
                else Liveness.UNKNOWN
            )
        finally:
            os.close(descriptor)
    except (OSError, ValueError):
        return Liveness.UNKNOWN


def start(
    host, name, launch, *, terminal, display=None, events=None, metadata=None
) -> ManagedProcess:
    host.validate(name, launch)
    generation = (metadata or {}).get("generation") or uuid.uuid4().hex
    if not GENERATION.fullmatch(generation):
        raise ConfigError("worker generation: invalid")
    private_directory(control_root())
    sweep(control_root())
    if not terminal:
        return _start_one_shot(host, name, launch, generation, metadata=metadata)
    directory = location(host.name, name)
    fresh = not os.path.lexists(directory)
    private_directory(directory)
    lock = lease_file(os.path.join(directory, "start.lock"))
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if os.path.lexists(os.path.join(directory, "current.json")) or not fresh:
            try:
                old = load_json(os.path.join(directory, "current.json"))["generation"]
            except (OSError, ValueError, KeyError) as error:
                raise Unanswered(f"worker {name}: previous generation unknown") from error
            if not isinstance(old, str) or liveness(host.name, name, old) != Liveness.ABSENT:
                raise SessionBusy(name, "previous worker generation (live or unknown)")
        run = os.path.join(directory, generation)
        os.mkdir(run, 0o700)
        save_json(os.path.join(directory, "current.json"), {"generation": generation})
        return _spawn(
            host,
            name,
            launch,
            run,
            terminal=True,
            display=display,
            events=events,
            metadata=metadata,
        )
    finally:
        os.close(lock)


def _start_one_shot(host, name, launch, generation, *, metadata) -> ManagedProcess:
    """In its one-shot dir, which owner.lock marks as this process's until its handle's close."""
    directory = one_shot(host.name, generation)
    if os.path.lexists(directory):
        raise ConfigError("worker generation: in use")
    private_directory(directory)
    owner = os.open(
        os.path.join(directory, "owner.lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )

    def remove() -> None:
        try:
            shutil.rmtree(directory, ignore_errors=True)
        finally:
            os.close(owner)

    try:
        fcntl.flock(owner, fcntl.LOCK_EX)
        run = os.path.join(directory, generation)
        os.mkdir(run, 0o700)
        handle = _spawn(host, name, launch, run, terminal=False, metadata=metadata)
    except BaseException:
        remove()
        raise
    handle.removal = remove
    return handle


def _spawn(
    host, name, launch, run, *, terminal, display=None, events=None, metadata=None
) -> TmuxManagedProcess:
    generation = os.path.basename(run)
    save_json(os.path.join(run, "state.json"), {"generation": generation, "state": "starting"})
    lease = lease_file(os.path.join(run, "lease"))
    try:
        fcntl.flock(lease, fcntl.LOCK_EX)
        argv = [
            sys.executable,
            "-I",
            guard.__file__,
            "--hook-socket",
            os.path.join(run, "hook"),
        ]
        if terminal:
            argv += ["--terminal", "--display-grace"]
            argv += ["--display-socket", os.path.join(run, "display")]
        argv += ["--", *launch.argv]
        # Launch.env passes verbatim; the PTY is no tmux pane, the display relays its bytes.
        environment = {
            **launch.env,
            GUARD_LEASE_FD: str(lease),
            GUARD_HOOK_SOCKET: os.path.join(run, "hook"),
        }
        for key in ("TMUX", "TMUX_PANE"):
            environment.pop(key, None)
        environment.setdefault("TERM", "xterm-256color")
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=launch.cwd,
                env=environment,
                close_fds=True,
                pass_fds=(lease,),
                start_new_session=True,
                bufsize=0,
            )
        except OSError as error:
            save_json(
                os.path.join(run, "state.json"), {"generation": generation, "state": "exited"}
            )
            raise SessionError(f"worker {name}: cannot start guard: {error}") from error
    finally:
        os.close(lease)

    def open_display() -> None:
        host.tmux.start(
            name,
            Launch(
                (
                    sys.executable,
                    "-I",
                    guard.__file__,
                    "--attach",
                    os.path.join(run, "display"),
                ),
                cwd=launch.cwd,
            ),
            events=events,
            status_line=host.tui_settings().status_line,
            tags={
                "@kind": "guard",
                "@generation": generation,
                **{
                    f"@{key}": value
                    for key, value in (metadata or {}).items()
                    if key in ("sid", "client")
                },
                **({"@display": display} if display is not None else {}),
            },
        )
        if sys.platform == "darwin":
            host.tmux.checked(
                ["set-hook", "-p", "-t", name, "pane-died", f"kill-session -t ={name}"]
            )

    handle = None
    try:
        assert proc.stdin is not None
        os.set_blocking(proc.stdin.fileno(), False)
        handle = TmuxManagedProcess(
            name,
            generation,
            proc,
            terminal=terminal,
            tmux=host.tmux,
            opener=open_display if terminal else None,
        )
        handle.await_start()
        if terminal:
            open_display()
    except BaseException:
        if handle is not None:
            with contextlib.suppress(SessionError):
                handle.close()
        else:
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.close()
            proc.stdout.close()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=GUARD_WAIT)
        raise
    return handle
