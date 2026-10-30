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
import time
import uuid
from collections.abc import Callable

from ensemble.session.engine.tmux_cli import display_error
from ensemble.session.engine.types import (
    SESSION_NAME,
    ConfigError,
    Liveness,
    SessionBusy,
    SessionError,
    Unanswered,
)
from ensemble.session.harness import guard
from ensemble.session.hosts.base import (
    GUARD_HOOK_SOCKET,
    GUARD_LEASE_FD,
    GUARD_WAIT,
    STATE_MAX,
    created_events,
)
from ensemble.session.hosts.managed import ManagedProcess
from ensemble.session.hosts.types import WorkerState

GENERATION = re.compile(r"[A-Za-z0-9_-]{1,64}")
ONE_SHOT = re.compile(r"o[0-9a-f]{15}")
SWEEP_AGE = 60  # seconds a new one-shot dir may take to lock its owner.lock
SOCKET_PATH_MAX = 103  # bytes: macOS's AF_UNIX sun_path holds 104 with its NUL, Linux's 108
LOG = logging.getLogger("session")


class TmuxManagedProcess(ManagedProcess):
    def __init__(self, name, generation, proc, *, terminal, start_reader=True):
        self.removal: Callable[[], None] | None = None
        super().__init__(name, generation, proc, terminal=terminal, start_reader=start_reader)

    def close(self) -> None:
        try:
            super().close()
        finally:
            if self.proc.poll() is not None:  # its guard gone: nothing holds the dir any more
                removal, self.removal = self.removal, None
                if removal is not None:
                    removal()


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
    root = os.environ.get("ENSEMBLE_CONTROL_ROOT", "/tmp")  # never macOS's long $TMPDIR
    return os.path.join(root, f"eg-{os.getuid()}")


def location(host: str, name: str) -> str:
    return os.path.join(control_root(), hashlib.sha256(f"{host}\0{name}".encode()).hexdigest()[:16])


def one_shot(host: str, generation: str) -> str:
    """A headless worker's guard dir: its generation's, never a tui's."""
    key = hashlib.sha256(f"{host}\0{generation}".encode()).hexdigest()[:15]
    return os.path.join(control_root(), f"o{key}")


def load_json(path: str, *, limit: int = 4096) -> dict:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > limit
        ):
            raise OSError("invalid guard record")
        value = json.loads(stream.read(limit + 1))
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
    """Whether another holds lock file `path` (its owner's exclusive lock; a probe's shared one
    never counts); absent: free.
    """
    try:
        descriptor = lease_file(path, create=False)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
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
        current = load_json(os.path.join(directory, "current.json"), limit=STATE_MAX)
        if current.get("generation") != generation:
            return Liveness.UNKNOWN
        run = os.path.join(directory, generation)
        check_directory(run)
        descriptor = lease_file(os.path.join(run, "lease"), create=False)
        try:
            try:  # shared: a concurrent probe is no live guard
                fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return Liveness.LIVE
            value = load_json(os.path.join(run, "state.json"))
            return Liveness.ABSENT if guard.ended(value, generation) else Liveness.UNKNOWN
        finally:
            os.close(descriptor)
    except (OSError, ValueError):
        return Liveness.UNKNOWN


def exited(host: str, name: str, generation: str) -> bool:
    """Whether run `generation`'s guard wrote its client exited, whatever its lease."""
    if not GENERATION.fullmatch(generation):
        return False
    directory = location(host, name)
    run = os.path.join(directory, generation)
    try:
        check_directory(directory)
        check_directory(run)
        value = load_json(os.path.join(run, "state.json"))
    except (OSError, ValueError):
        return False
    return guard.exited(value, generation)


def records(host: str) -> dict[str, WorkerState]:
    root = control_root()
    if not os.path.lexists(root):
        return {}
    found = {}
    try:
        check_directory(root)
        for entry in sorted(os.listdir(root)):
            if not re.fullmatch(r"[0-9a-f]{16}", entry):
                continue
            directory = os.path.join(root, entry)
            check_directory(directory)
            record = load_json(os.path.join(directory, "current.json"), limit=STATE_MAX)
            if set(record) == {"generation"}:  # old records still gate starts by their lease
                continue
            if record.get("host") != host:
                continue
            name, generation = record.get("name"), record.get("generation")
            if (
                not isinstance(name, str)
                or not SESSION_NAME.fullmatch(name)
                or directory != location(host, name)
                or not isinstance(generation, str)
                or not GENERATION.fullmatch(generation)
                or any(
                    record.get(key) is not None and not isinstance(record[key], str)
                    for key in ("sid", "client", "display")
                )
            ):
                raise ValueError("invalid worker identity")
            found[name] = WorkerState(
                record.get("sid"),
                generation,
                record.get("client"),
                record.get("display"),
                None,
                host,
            )
    except (OSError, ValueError) as error:
        raise Unanswered(f"host {host}: guard inventory: {error}") from error
    return found


def start(
    host, name, launch, *, terminal, display=None, events=None, metadata=None
) -> ManagedProcess:
    host.validate(name, launch)
    if why := display_error(display):
        raise ConfigError(why)
    created_events(events)
    generation = (metadata or {}).get("generation") or uuid.uuid4().hex
    if not GENERATION.fullmatch(generation):
        raise ConfigError("worker generation: invalid")
    record = {
        "host": host.name,
        "name": name,
        "generation": generation,
        "sid": (metadata or {}).get("sid"),
        "client": (metadata or {}).get("client"),
        "display": display,
    }
    if terminal and len(json.dumps(record)) > STATE_MAX:
        raise ConfigError(f"worker {name}: guard identity exceeds {STATE_MAX} bytes")
    directory = location(host.name, name) if terminal else one_shot(host.name, generation)
    socket = os.path.join(directory, generation, "display" if terminal else "hook")
    if (size := len(os.fsencode(socket))) > SOCKET_PATH_MAX:
        raise ConfigError(
            f"guard socket {socket}: {size} bytes, over the {SOCKET_PATH_MAX}-byte AF_UNIX limit;"
            " set a shorter ENSEMBLE_CONTROL_ROOT"
        )
    private_directory(control_root())
    sweep(control_root())
    if not terminal:
        return _start_one_shot(host, name, launch, generation)
    fresh = not os.path.lexists(directory)
    private_directory(directory)
    lock = lease_file(os.path.join(directory, "start.lock"))
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if os.path.lexists(os.path.join(directory, "current.json")) or not fresh:
            try:
                old = load_json(os.path.join(directory, "current.json"), limit=STATE_MAX)[
                    "generation"
                ]
            except (OSError, ValueError, KeyError) as error:
                raise Unanswered(f"worker {name}: previous generation unknown") from error
            if not isinstance(old, str) or liveness(host.name, name, old) != Liveness.ABSENT:
                raise SessionBusy(name, "previous worker generation (live or unknown)")
            with contextlib.suppress(FileNotFoundError):
                os.unlink(os.path.join(directory, old, "diagnostic.json"))
        run = os.path.join(directory, generation)
        os.mkdir(run, 0o700)
        save_json(
            os.path.join(directory, "current.json"),
            record,
        )
        return _spawn(host, name, launch, run, terminal=True)
    finally:
        os.close(lock)


def _start_one_shot(host, name, launch, generation) -> ManagedProcess:
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
        handle = _spawn(host, name, launch, run, terminal=False)
    except BaseException:
        remove()
        raise
    handle.removal = remove
    return handle


def _spawn(host, name, launch, run, *, terminal) -> TmuxManagedProcess:
    generation = os.path.basename(run)
    settings = host.tui_settings()
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
            argv += ["--size", "{}x{}".format(*settings.hidden_size)]
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

    handle = None
    try:
        assert proc.stdin is not None
        os.set_blocking(proc.stdin.fileno(), False)
        handle = TmuxManagedProcess(
            name,
            generation,
            proc,
            terminal=terminal,
        )
        handle.await_start()
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
