"""The tui mode: a Client command on a Host terminal; events from its client's own record,
then the hook records appended to its session directory, and from its terminal.
"""

import contextlib
import json
import logging
import os
import re
import signal
import stat
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import BinaryIO, Self

from ensemble.session.clients.base import Client
from ensemble.session.clients.registry import Registry
from ensemble.session.engine.status_log import SessionStatusLog
from ensemble.session.engine.types import (
    COMMAND,
    PROCESS_EXIT,
    SESSION_NAME,
    SESSION_NAME_RULE,
    Access,
    Activity,
    Blocked,
    ClientBound,
    ConfigError,
    HookError,
    HookKind,
    Key,
    KeyPress,
    Launch,
    Liveness,
    Mode,
    NotShowable,
    Paste,
    Prepared,
    SessionBusy,
    SessionError,
    SessionEvent,
    SessionInfo,
    Spec,
    StatusState,
    Stderr,
    TurnEnd,
    TurnFailed,
    TurnStarted,
    Unanswered,
    WorkerHook,
)
from ensemble.session.hosts.base import (
    ACTIVITY,
    GUARD_WAIT,
    HARNESS_ROOT,
    PROMPTED,
    RECORD,
    TUI_DIR,
    TUI_STATE,
    Host,
    InteractiveProcess,
    Process,
    activity,
    check_sid,
    checked_events,
    created_events,
    sent,
    tui_lock,
    worker_state,
)
from ensemble.session.hosts.managed import ManagedProcess
from ensemble.session.modes import tui_hook
from ensemble.session.modes.base import BaseMode, identity_drop
from ensemble.session.modes.client_adapters.tui import Tui
from ensemble.session.viewer.viewer import printed
from ensemble.utils import CONFIG_NAME as NAME
from ensemble.utils import (
    STALE_AGE,
    Display,
    command_path,
    idle,
    inode_lock,
    json_records,
    read_regular,
    save,
)
from ensemble.utils import Record as ControlRecord

WORDS = {
    HookKind.TURN_END: StatusState.DONE,
    HookKind.TURN_FAILED: StatusState.DONE,
    HookKind.BLOCKED: StatusState.BLOCKED,
    HookKind.ACTIVITY: StatusState.WORKING,
    HookKind.TURN_STARTED: StatusState.WORKING,
}
SLICE = 0.1
OPEN_AFTER = 1.0  # keys sent before a client's input box shows are lost
POLL_SCREEN = 0.5
SEND_LOCK = "send.lock"
LOG = logging.getLogger("session")
Cursor = tuple[int | None, int]  # inode, offset
Record = tuple[str, dict, str | None]  # label, payload, sent
OWNER = re.compile(r"[0-9a-f]{32}")
RECHECK = 0.1
GATE_BUSY = "previous worker generation (live or unknown)"
CLOSE_BUSY = "worker generation (live or unknown)"


def checked_name(name) -> str:
    if not isinstance(name, str) or not SESSION_NAME.fullmatch(name):
        raise ConfigError(f"name must match {SESSION_NAME_RULE}, got {name!r:.80}")
    return name


def _state(
    prepared: Prepared,
    run: Tui,
    host: Host,
    metadata: Mapping[str, str],
    owner: str | None,
    *,
    display: Display,
) -> dict:
    spec = prepared.spec
    return {
        "client": spec.client,
        "workdir": spec.workdir,
        **prepared.info.persisted(),
        "events": spec.events,
        "words": {label: WORDS[kind] for label, kind in run.hooks.items() if kind in WORDS},
        "host": host.name,
        "generation": metadata["generation"],
        "display": display,
        **({"client_hook": hook.fields()} if (hook := run.hook_callback()) is not None else {}),
        **({"owner": owner} if owner is not None else {}),
    }


def _owner_ok(path) -> bool:
    """<real HARNESS_ROOT>/<32 lowercase hex> only."""
    if not isinstance(path, str) or not os.path.isabs(path) or not path.isprintable():
        return False
    name = os.path.basename(path)
    root = os.path.realpath(os.path.expanduser(HARNESS_ROOT))
    return bool(OWNER.fullmatch(name)) and os.path.realpath(path) == os.path.join(root, name)


def _bad(state: Mapping) -> str | None:
    """The first session.json field its readers refuse."""
    for field, check in tui_hook.SESSION_STATE.items():
        if field not in state or not check(state[field]):
            return field
    generation = state.get("generation")
    if not isinstance(generation, str) or not generation.isprintable() or not generation:
        return "generation"
    client = state.get("client")
    if not isinstance(client, str) or not NAME.match(client):
        return "client"
    try:
        SessionInfo.from_fields(state, strict=True)
    except ValueError as e:
        return str(e)
    if "owner" in state and not _owner_ok(state["owner"]):
        return "owner"
    return None


def _sends(marker: str | None, generation: str) -> tuple[int, int]:
    held, begun, submitted = [*(marker or "").split(), "", "", ""][:3]
    if held != generation or not begun.isdigit() or not submitted.isdigit():
        return 0, 0
    return int(begun), int(submitted)


def _read_state(name: str, path: str) -> dict:
    try:
        state = tui_hook.read_state(path, error=HookError)
    except HookError as e:
        raise SessionError(f"no tui session {name}: {e}") from None
    if field := _bad(state):
        raise SessionError(f"no tui session {name}: {path}: bad {field}")
    return state


class TuiStore:
    """Tui session `name`'s dir and lock under TUI_DIR; what changes them takes the lock.
    attach(name, host=...) takes by name whatever is there: a tui session, a half-gone one (its dir
    or its host session alone), a non-tui host session or nothing. close: under its lock, removes
    the dir; absent is fine. An owned record goes only with its own worker generation: by its
    owner's handle, else once that generation's worker_liveness is ABSENT, its leftovers released
    through Host._release. With no owned record, the host's session of that name (a delegate
    drive's) is closed.
    """

    def __init__(self, name: str, *, host: Host, sid: str | None = None) -> None:
        self.name, self.host = name, host
        self._sid = sid
        self.root = os.path.expanduser(TUI_DIR)
        self.dir = os.path.join(self.root, name)
        self.state_file = os.path.join(self.dir, TUI_STATE)

    @classmethod
    def attach(cls, name: str, *, host: Host, sid: str | None = None) -> Self:
        return cls(checked_name(name), host=host, sid=sid)

    def hook_argv(self) -> tuple[str, ...]:
        """The client's hook command: the running plugin's ensemble-session (where PATH finds it)
        and this dir; the client appends its label.
        """
        command = command_path(COMMAND)
        if command is None:
            raise ConfigError(
                f"tui session {self.name}: {COMMAND} is not on PATH (its hooks run it)"
            )
        return (command, "hook", self.dir)

    def set_activity(self, generation: str, word: StatusState, events: str | None) -> None:
        """Its owner's mark: the activity file, then for done and blocked one events line."""
        save(os.path.join(self.dir, ACTIVITY), f"{generation} {word}\n")
        if word in (StatusState.DONE, StatusState.BLOCKED) and events:
            SessionStatusLog(events).write(self.name, word)

    def store_record(self, generation: str, record: ControlRecord) -> None:
        """Its crew Record, for `generation` while session.json names it; else SessionError. Only
        the crew Worker owning that Record calls it.
        """
        found = worker_state(self.name)
        if found is None or found.generation != generation:
            raise SessionError(f"session {self.name} is gone")
        path = os.path.join(self.dir, RECORD)
        try:
            fd, temporary = tempfile.mkstemp(dir=self.dir, prefix=f".{RECORD}-")  # 0600, O_NOFOLLOW
        except FileNotFoundError:
            raise SessionError(f"session {self.name} is gone") from None
        except OSError as e:
            raise SessionError(f"{path}: {e.strerror or e}") from None
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(f"{generation}\t{record.to_line()}\n".encode())
            os.replace(temporary, path)
        except OSError as e:
            raise SessionError(f"{path}: {e.strerror or e}") from None
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)  # left only when not replaced

    def read(self) -> dict:
        return _read_state(self.name, self.state_file)

    def records(self, cursor: Cursor | None) -> tuple[list[tuple[str, dict]], Cursor]:
        found, end = self.stamped(cursor)
        return [(label, payload) for label, payload, _ in found], end

    def stamped(self, cursor: Cursor | None) -> tuple[list[Record], Cursor]:
        """The hook log's records past `cursor` (None: ROTATED's too); the cursor past them.
        shortcut: two rotations between reads lose the middle file.
        """
        try:
            return self._records(cursor)
        except OSError:
            return [], cursor or (None, 0)

    def _records(self, cursor: Cursor | None) -> tuple[list[Record], Cursor]:
        with _opened(os.path.join(self.dir, tui_hook.HOOKS)) as (ino, current):
            older = b""
            if cursor is None or cursor[0] != ino:
                with _opened(os.path.join(self.dir, tui_hook.ROTATED)) as (old, stream):
                    if stream is not None and old != ino and (cursor is None or old == cursor[0]):
                        stream.seek(0 if cursor is None else cursor[1])
                        older = stream.read()
                start = 0
            else:
                start = cursor[1]
            data = b""
            if current is not None:
                current.seek(start)
                data = current.read()
        found, _ = _parsed(older)
        new, end = _parsed(data)
        return found + new, (ino, start + end)

    def hosted(self) -> Process | None:
        """Its host's session of its name, checked against its sid: a view's terminal or, with no
        worker state, a non-tui one (a delegate drive's); never a gone worker's leftovers.
        """
        process = self.host.attach(self.name) if self.name in self.host.list() else None
        check_sid(self.name, process, self._sid)
        return process

    @contextlib.contextmanager
    def sending(self, *, wait: bool = True) -> Iterator[bool]:
        """Holds the send lock; wait False yields False while another holds it."""
        path = os.path.join(self.dir, SEND_LOCK)
        with contextlib.ExitStack() as stack:
            try:
                held = stack.enter_context(inode_lock(path, wait=wait))
            except FileNotFoundError:
                raise SessionError(f"tui session {self.name} is not running") from None
            except OSError as e:
                raise SessionError(f"{path}: {e.strerror or e}") from None
            yield held

    def prompts(self, generation: str) -> tuple[int, int]:
        """Sends to `generation` begun, submitted."""
        return _sends(sent(self.dir), generation)

    def mark_prompted(self, generation: str, *, submitted: bool = False) -> None:
        """Under the send lock: one more begun, or all begun submitted."""
        path, (begun, done) = os.path.join(self.dir, PROMPTED), self.prompts(generation)
        begun, done = (begun, begun) if submitted else (begun + 1, done)
        try:
            fd = os.open(
                path + ".new", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(fd, "wb") as f:
                f.write(f"{generation} {begun} {done}".encode())
            os.replace(path + ".new", path)
        except OSError as e:
            LOG.warning("tui session %s: %s: %s", self.name, path, e.strerror or e)

    def check(self) -> None:
        """Worker state of another sid raises; none does not."""
        found = worker_state(self.name)
        if self._sid is not None and found is not None and found.sid != self._sid:
            raise SessionError(
                f"session {self.name}: expected sid {self._sid!r}, found {found.sid!r}"
            )

    def start(
        self,
        state: dict,
        commit: Callable[[], None],
        launch: Callable[[], InteractiveProcess],
        *,
        check_name: Callable[[str], None] | None = None,
    ) -> InteractiveProcess:
        if field := _bad(state):
            raise SessionError(f"tui session {self.name}: bad {field} {state.get(field)!r:.80}")
        with self._locked():
            if check_name is not None:
                check_name(self.name)
            made = self._make(state)
            try:
                commit()
                return launch()
            except (ConfigError, SessionError):
                self._unmake(made)
                raise

    def close(self, handle: Process | None = None, generation: str | None = None) -> bool:
        """generation: the owner `handle`'s, else the caller's (None: the name's). Whether the dir
        went.
        """
        with self._locked():
            record = _owned_record(self)
            if handle is not None:
                return self._close_own(handle, generation, record)
            if record is not None:
                return self._close_other(record, generation)
            self._close()
            self._forget()
            return True

    def tracked(self) -> tuple[str, str]:
        return self.dir, os.path.join(self.dir, tui_hook.HOOKS)

    def expire(self, listed: set[str]) -> None:
        """Its dir and lock go once idle with no session of its name on its host; never waits."""
        lock = tui_lock(self.name)
        with inode_lock(lock, wait=False) as held:
            if not held or not idle(self.tracked(), STALE_AGE) or self.name in listed:
                return
            try:
                state = json.loads(read_regular(self.state_file) or b"{}")
            except ValueError:
                state = {}
            if isinstance(state, dict) and state.get("host", self.host.name) != self.host.name:
                return
            if (record := _owned_record(self)) is not None and _generation(
                self, record, wait=False
            ) != Liveness.ABSENT:
                return
            self._drop()
            os.unlink(lock)

    def stop(
        self, terminal: Process, *, owned: bool = False, generation: str | None = None
    ) -> None:
        """owned: closes `terminal`, its owner's handle. Else an owned record goes by the record
        rule; with none, there is no worker to end: only its dir goes.
        """
        if owned:
            self.close(terminal, generation)
            return
        with self._locked():
            if (record := _owned_record(self)) is not None:
                self._close_other(record, generation)
                return
            self._drop()
            self._forget()

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self._root()
        path = tui_lock(self.name)
        with contextlib.ExitStack() as stack:
            try:
                stack.enter_context(inode_lock(path))
            except OSError as e:
                raise SessionError(f"{path}: {e.strerror or e}") from None
            yield

    def _forget(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tui_lock(self.name))

    def _root(self) -> None:
        try:
            os.makedirs(self.root, 0o700, exist_ok=True)
        except OSError as e:
            raise SessionError(f"{self.root}: {e.strerror or e}") from None

    def _make(self, state: dict) -> os.stat_result:
        """A fresh dir with session.json, unless the session runs or is unknown; the dir's stat."""
        stale = _stale(self)
        if stale is not None:
            self.host._release(self.name, stale)
        folder = self.dir
        try:
            if os.path.lexists(folder):
                if not stat.S_ISDIR(os.lstat(folder).st_mode):
                    raise SessionError(f"{folder}: not a directory")
                self._remove()
            os.mkdir(folder, 0o700)
            made = os.lstat(folder)
        except FileExistsError:
            raise SessionError(f"tui session {self.name} is starting") from None
        except OSError as e:
            raise SessionError(f"{folder}: {e.strerror or e}") from None
        try:
            fd, temp = tempfile.mkstemp(dir=folder, prefix=".session.")
            with os.fdopen(fd, "w") as f:
                json.dump(state, f)
            os.replace(temp, self.state_file)
        except OSError as e:
            self._unmake(made)
            raise SessionError(f"{folder}: {e.strerror or e}") from None
        return made

    def _close_own(self, handle: Process, generation: str | None, record: dict | None) -> bool:
        if isinstance(handle, ManagedProcess):
            handle.close_display()  # first: its pane dying is no exit to report
        failed = handle.state() == Liveness.UNKNOWN
        if not failed:
            try:
                handle.close()
            except Unanswered:
                failed = True
            except SessionError as error:
                raise SessionError(f"cannot close tui session {self.name}: {error}") from error
        if failed:
            raise Unanswered(f"worker {self.name}: exit unconfirmed")
        if record is None or record.get("generation") != generation:
            return False
        self._drop()
        self._forget()
        return True

    def _close_other(self, record: dict, generation: str | None) -> bool:
        if generation is not None and record.get("generation") != generation:
            return False
        if _generation(self, record, wait=True) != Liveness.ABSENT:
            raise SessionBusy(self.name, CLOSE_BUSY)
        self._shut(record["generation"])
        self._forget()
        return True

    def _close(self) -> None:
        """No owned record: its host's session of its name, if any, closed; then its dir."""
        try:
            if (process := self.hosted()) is not None:
                process.close()
        except SessionError as error:
            raise SessionError(f"cannot close tui session {self.name}: {error}") from error
        self._drop()

    def _shut(self, generation: str) -> None:
        """Its ABSENT worker generation's leftovers released through its host; then its dir."""
        try:
            self.host._release(self.name, generation)
        except SessionError as error:
            raise SessionError(f"cannot close tui session {self.name}: {error}") from error
        self._drop()

    def _drop(self) -> None:
        try:
            self._remove()
        except OSError as e:
            raise SessionError(f"cannot close tui session {self.name}: {e.strerror or e}") from None

    def _remove(self, made: os.stat_result | None = None) -> None:
        try:
            st = os.lstat(self.dir)
        except FileNotFoundError:
            return
        if stat.S_ISDIR(st.st_mode) and (made is None or os.path.samestat(st, made)):
            # A hook may outlive its terminal; retire its public path before deleting files.
            with tempfile.TemporaryDirectory(dir=self.root, prefix=".closed-") as retired:
                os.rename(self.dir, retired)

    def _unmake(self, made: os.stat_result) -> None:
        with contextlib.suppress(OSError):
            self._remove(made)


@contextlib.contextmanager
def _opened(path: str) -> Iterator[tuple[int | None, BinaryIO | None]]:
    """`path`'s inode and stream if regular, else (None, None); no FIFO wait."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        yield None, None
        return
    try:
        found = os.fstat(fd)
        if not stat.S_ISREG(found.st_mode):
            yield None, None
            return
        with os.fdopen(fd, "rb", closefd=False) as stream:
            yield found.st_ino, stream
    finally:
        os.close(fd)


def _parsed(data: bytes) -> tuple[list[Record], int]:
    end = data.rfind(b"\n") + 1
    found = []
    for record in json_records(data[:end].split(b"\n")):
        label, payload, sent = record.get("label"), record.get("payload"), record.get("sent")
        if isinstance(label, str) and isinstance(payload, dict):
            found.append((label, payload, sent if isinstance(sent, str) else None))
    return found, end


def _sweep(store: TuiStore) -> None:
    """Other names idle STALE_AGE: expired, errors logged; one host listing, once one is idle."""
    try:
        found = {entry.name.removesuffix(".lock") for entry in os.scandir(store.root)}
    except OSError as error:
        LOG.warning("tui sweep: %s", error)
        return
    listed = None
    for name in sorted(found - {store.name}):
        if not SESSION_NAME.fullmatch(name):
            continue
        other = TuiStore.attach(name, host=store.host)
        try:
            if os.path.islink(other.dir) or not idle((*other.tracked(), tui_lock(name)), STALE_AGE):
                continue
            if listed is None:
                try:
                    listed = set(store.host.list())
                except (OSError, SessionError) as error:
                    LOG.warning("tui sweep: %s", error)
                    return
            other.expire(listed)
        except (OSError, SessionError) as error:
            LOG.warning("tui sweep %s: %s", name, error)


def _owned_record(store: TuiStore) -> dict | None:
    """session.json naming an owner; unreadable but naming one: its generation unknown."""
    try:
        record = store.read()
    except SessionError:
        try:
            raw = json.loads(read_regular(store.state_file) or b"{}")
        except ValueError:
            return None
        return {"owner": raw["owner"]} if isinstance(raw, dict) and "owner" in raw else None
    return record if "owner" in record else None


def _generation(store: TuiStore, record: Mapping, *, wait: bool) -> Liveness:
    """wait: LIVE re-checked for up to GUARD_WAIT s (a reaping guard)."""
    generation = record.get("generation")
    if not isinstance(generation, str):
        return Liveness.UNKNOWN
    deadline = time.monotonic() + GUARD_WAIT
    while True:
        try:
            found = store.host.worker_liveness(store.name, generation)
        except SessionError:
            return Liveness.UNKNOWN
        if not wait or found != Liveness.LIVE or time.monotonic() >= deadline:
            return found
        time.sleep(RECHECK)


def _running(name: str, host: Host) -> SessionError:
    target = host.attach_target(name)
    hint = "" if target is None else f"; {printed(target)}"
    return SessionError(f"tui session {name} is running{hint}")


def _stale(store: TuiStore) -> str | None:
    """Tui session `store.name`'s ended worker generation, whose leftovers go before a start, else
    None (no owned record: no worker); SessionError while it runs, by its owned record's
    generation (never its display), SessionBusy while that is unknown.
    """
    record = _owned_record(store)
    if record is None:
        return None
    liveness = _generation(store, record, wait=True)
    if liveness == Liveness.UNKNOWN:
        raise SessionBusy(store.name, GATE_BUSY)
    if liveness == Liveness.LIVE:
        raise _running(store.name, store.host)
    return record["generation"]


@dataclass(frozen=True)
class _Start:
    prepared: Prepared
    run: Tui
    store: TuiStore
    state: dict
    metadata: Mapping[str, str]
    prompt_file: tuple[str, str] | None = None  # (path, text) this mode writes

    def validate(self) -> None:
        self.store.host._validate(self.store.name, self.launch, self.metadata)

    def preflight(self) -> None:
        self.store.host._preflight(self.store.name, self.launch, self.metadata)

    @property
    def launch(self) -> Launch:
        info = self.prepared.info
        return Launch(
            self.prepared.argv, env=self.prepared.env, cwd=info.cwd, transcript=info.transcript
        )


class TuiMode(BaseMode):
    """One tui session holding its terminal generation."""

    kind = Mode.TUI

    @staticmethod
    def resolve_name(spec: Spec) -> str:
        if spec.sid is None:
            raise ConfigError("sid: missing")
        return checked_name(f"{spec.client}-{spec.sid[:8]}" if spec.name is None else spec.name)

    def __init__(
        self,
        prepared: Prepared,
        run: Tui,
        terminal: InteractiveProcess,
        store: TuiStore,
        generation: str,
        *,
        cursor: Cursor = (None, 0),
        replayed: tuple[SessionEvent | Stderr | ClientBound, ...] = (),
        opening: tuple[str, str] | None = None,
        owned: bool = False,
    ) -> None:
        """cursor: its next hook record (TuiStore.records); owned: `terminal` is the owner's (its
        harness's start), which alone marks its worker's activity; else a view.
        """
        super().__init__(prepared)
        self.name, self.run, self.host, self._terminal = store.name, run, store.host, terminal
        self._store, self._replayed, self._generation = store, replayed, generation
        self._cursor, self._dialogs, self._limit, self._code = cursor, frozenset(), None, None
        self._live, self._gone = True, False
        self._opening = opening  # start's: sent once, OPEN_AFTER s after
        self._due: float | None = None  # its label's latest record
        self._held, self._checked = False, -POLL_SCREEN
        self._active_turn = run._turn_open is True
        self._absorbed, self._launching = 0, bool(prepared.spec.prompt) and not run._opening_prompt
        self._owned, self._hook_logged = owned, False

    @classmethod
    def preflight(
        cls,
        spec: Spec,
        client: Client,
        *,
        registry: Registry,
        host: Host,
        display: Display = None,
    ) -> Prepared:
        pending = cls._prepare(spec, client, registry=registry, host=host, display=display)
        try:
            pending.preflight()
            _stale(pending.store)
        except SessionBusy:
            raise
        except SessionError as error:
            raise ConfigError(str(error)) from error
        return pending.prepared

    @classmethod
    def _prepare(
        cls,
        spec: Spec,
        client: Client,
        *,
        registry: Registry,
        host: Host,
        display: Display,
        owner: str | None = None,
    ) -> _Start:
        run = client.tui()
        if run is None:
            raise cls._unsupported(spec)
        name = cls.resolve_name(spec)
        spec, account = cls._selected(spec, client)
        events = checked_events(spec.events)
        drop = identity_drop(registry)
        store = TuiStore.attach(name, host=host)
        spec, metadata = cls._hosted(
            replace(spec, name=name, events=events), run._home(), host, name, account
        )
        spec = cls._noted(spec, client)
        run._context = host._client_context()
        spec, prompt_file = cls._pointed(spec, run._context, store.dir)
        if prompt_file and run._context.container:
            path, text = prompt_file
            metadata, prompt_file = {**metadata, "prompt_file": path, "prompt": text}, None
        hook = store.hook_argv()
        prepared = cls._launched(
            spec, lambda s: run.launch(s, hook), drop, inherit=host._inherit_env
        )
        prepared = cls._host_resume(prepared, host, name)
        if run._runtime_files:
            metadata = {**metadata, "runtime_files": json.dumps(dict(run._runtime_files))}
        if owner is not None:
            metadata = {**metadata, "owner": owner}
        state = _state(prepared, run, host, metadata, owner, display=display)
        if field := _bad(state):
            raise SessionError(f"tui session {name}: bad {field} {state.get(field)!r:.80}")
        return _Start(prepared, run, store, state, metadata, prompt_file)

    @classmethod
    def start(
        cls,
        spec: Spec,
        client: Client,
        *,
        registry: Registry,
        record: Callable[[SessionInfo], None],
        host: Host,
        display: Display = None,
        check_name: Callable[[str], None] | None = None,
        owner: str | None = None,
    ) -> Self:
        """Start the Client's terminal with its hook journal committed before execution; owner: its
        session harness's dir, kept in session.json.
        """
        pending = cls._prepare(
            spec, client, registry=registry, host=host, display=display, owner=owner
        )
        prepared, run, store = pending.prepared, pending.run, pending.store
        try:
            pending.validate()
            created_events(prepared.spec.events)
        except SessionBusy:
            raise
        except SessionError as error:
            raise SessionError(f"cannot start tui session {store.name}: {error}") from error

        def launch() -> InteractiveProcess:
            try:
                if pending.prompt_file:
                    cls._write_prompt(*pending.prompt_file)
                return host.worker(
                    store.name,
                    pending.launch,
                    terminal=True,
                    display=display,
                    events=prepared.spec.events,
                    metadata=pending.metadata,
                )
            except SessionBusy:
                raise
            except SessionError as error:
                raise SessionError(f"cannot start tui session {store.name}: {error}") from error

        def commit() -> None:
            run.commit()
            record(prepared.info)

        terminal = store.start(pending.state, commit, launch, check_name=check_name)
        _sweep(store)
        generation = pending.state["generation"]
        return cls(
            prepared,
            run,
            terminal,
            store,
            generation,
            opening=run.opening,
            owned=True,
        )

    @classmethod
    def running(cls, name: str, *, host: Host) -> None:
        raise _running(checked_name(name), host)

    @classmethod
    def snapshot(cls, name: str) -> dict:
        """Tui session `name`'s session.json as start wrote it; else SessionError."""
        return _read_state(
            checked_name(name), os.path.join(os.path.expanduser(TUI_DIR), name, TUI_STATE)
        )

    @classmethod
    def attach(
        cls, name: str, state: Mapping, client: Client, *, host: Host, sid: str | None = None
    ) -> Self:
        """Tui session `name` from its snapshot() if its terminal is tagged with its sid; a fresh Tui
        restored replays its client's record, then its hook log; events from the next record.
        """
        store = TuiStore.attach(name, host=host, sid=sid)
        if sid is not None and state.get("sid") != sid:
            raise SessionError(
                f"session {name}: expected sid {sid!r}, found {state.get('sid')!r} in session.json"
            )
        info = SessionInfo.from_fields(state)
        if info is None:
            raise SessionError(f"no tui session {name}: {store.state_file}: bad {_bad(state)}")
        try:
            run = client.tui()
        except ConfigError as e:
            raise SessionError(f"no tui session {name}: {e}") from None
        if run is None:
            raise SessionError(f"no tui session {name}: client {state['client']!r} has no tui mode")
        terminal = store.hosted()
        if terminal is not None and not isinstance(terminal, InteractiveProcess):
            raise SessionError(f"session {name} has no terminal")
        if terminal is None or terminal.session_identity() != (info.sid, state["generation"]):
            raise SessionError(f"no tui session {name}")
        spec = Spec(
            workdir=state["workdir"],
            access=Access(cwd=info.cwd),
            client=state["client"],
            mode=Mode.TUI,
            sid=info.sid,
            name=name,
            events=state["events"],
        )
        try:
            run._context = host._client_context()
            run.restore(spec, state["client_hook"]["state"] if "client_hook" in state else {})
        except ConfigError as e:
            raise SessionError(f"no tui session {name}: {e}") from None
        records, cursor = store.records(None)
        replayed = run.replay(records)
        return cls(
            Prepared(spec, info),
            run,
            terminal,
            store,
            state["generation"],
            cursor=cursor,
            replayed=(*replayed,),
        )

    @property
    def generation(self) -> str:
        return self._generation

    @property
    def remote(self) -> bool:
        return getattr(self._terminal, "remote", False) is True

    @property
    def client_up(self) -> bool:
        return getattr(self._terminal, "up", False) is True

    @property
    def replayed(self) -> tuple[SessionEvent | Stderr | ClientBound, ...]:
        """Attached: its client record's, then its hook log's events so far."""
        return self._replayed

    @property
    def opening_pending(self) -> str | None:
        return None if self._opening is None or self._gone else self._opening[1]

    def marked_blocked(self) -> bool:
        """False, too, when its terminal cannot tell."""
        return activity(self.name, self._generation) == StatusState.BLOCKED

    def poll(self, timeout: float) -> tuple[list[SessionEvent | Stderr | ClientBound], bool]:
        self._drain()
        found = self._read()
        if not found and not self._gone:
            state = self._terminal.state()
            if not self._replaced(state) and state != Liveness.UNKNOWN:
                code = self._terminal.returncode()
                if state == Liveness.ABSENT and code is None:  # an owner's: removed unconfirmed
                    code = -signal.SIGHUP
                if code is not None:
                    self._code = code
                    final = self._read()
                    if self._active_turn:
                        final.append(
                            TurnFailed(
                                PROCESS_EXIT if code else "incomplete_turn",
                                f"worker exited {code}",
                            )
                        )
                        self._active_turn = False
                    self._end()
                    return final, True
                with contextlib.suppress(SessionError):
                    screen = self._terminal.screen()
                    found = self._read()
                    if not found and not self._gone and not self._replaced(self._terminal.state()):
                        found = self._scan(screen)
        self._open()
        deadline = time.monotonic() + timeout
        while not found and not self._gone and (left := deadline - time.monotonic()) > 0:
            time.sleep(min(SLICE, left))
            found = self._read()
            self._open()
        return found, self._gone

    def _drain(self) -> None:
        if not self._owned:
            return
        for message in self._terminal.read(0):  # its exit code: the handle's returncode()
            if isinstance(message, WorkerHook) and not self._hook_logged:
                self._hook_logged = True
                LOG.warning(
                    "tui session %s: hook %r over the guard channel ignored; hooks use hooks.jsonl",
                    self.name,
                    message.label,
                )

    def _open(self) -> None:
        """Submits the opening once due, no turn runs and a turn boundary's hook saw every send,
        unless a dialog shows, the client is blocked or the send lock is held.
        """
        now = time.monotonic()
        if self._opening is None or self._due is None or self._gone or now < self._due:
            return
        if self._held or self._active_turn or self._launching or now < self._checked + POLL_SCREEN:
            return
        self._checked = now
        try:
            if self._shown(self._terminal.screen()):
                return
            # may be cached until alive()
            if not self._terminal.alive() or self._shown(self._terminal.screen()):
                return
        except NotShowable:
            pass
        except SessionError:
            return
        with contextlib.suppress(SessionError), self._store.sending(wait=False) as held:
            if not held or self._store.prompts(self._generation)[0] > self._absorbed:
                return
            text, self._opening = self._opening[1], None
            self._running()
            self._deliver(self.run.submit(text), prompted=False)

    def _track(self, events: list[SessionEvent | Stderr | ClientBound]) -> None:
        for event in events:
            if isinstance(event, TurnStarted):
                self._active_turn, self._launching = True, False
            elif isinstance(event, TurnEnd | TurnFailed):
                self._active_turn = False
            if isinstance(event, Blocked | Activity | TurnEnd | TurnFailed | TurnStarted):
                self._held = isinstance(event, Blocked)

    def _end(self) -> None:
        self._gone = True
        self._code = -signal.SIGHUP if self._code is None else self._code

    def _read(self) -> list[SessionEvent | Stderr | ClientBound]:
        """New records' events, the client's own first (read last, so a text written before its
        hook record precedes it), checked against the terminal and session.json only once found.
        """
        if self._gone:
            return []
        records, end = self._store.stamped(self._cursor)
        observed = self.run.observe()
        if end == self._cursor and not observed or self._replaced(self._terminal.state()):
            return []
        self._cursor = end
        if self._opening is not None and any(label == self._opening[0] for label, *_ in records):
            self._due = time.monotonic() + OPEN_AFTER
        found: list[SessionEvent | Stderr | ClientBound] = [*observed]
        for label, payload, marker in records:
            if (word := WORDS.get(self.run.hooks.get(label))) is not None:
                self._mark(word)
            events = self.run.hook_events(label, payload)
            if any(isinstance(e, TurnStarted | TurnEnd | TurnFailed) for e in events):
                self._absorbed = max(self._absorbed, _sends(marker, self._generation)[1])
            found += events
        self._track(found)
        return found

    def _replaced(self, state: Liveness) -> bool:
        """Ended once another terminal holds its name or session.json is another generation's."""
        if self._owned:
            return False
        if state == Liveness.ABSENT:
            try:
                replacement = self._store.hosted()
            except SessionError:
                self._end()
                return True
            if replacement is not None and replacement.identity != self._terminal.identity:
                self._end()
                return True
        try:
            current = self._store.read()
        except SessionError:
            self._end()
            return True
        if current["generation"] != self._generation:
            self._end()
            return True
        return False

    def _scan(self, screen: str) -> list[SessionEvent | Stderr | ClientBound]:
        """Since the last scan: Activity("") once a dialog went, Blocked per new dialog, LimitHit
        per appearance.
        """
        shown = self._shown(screen)
        screen = " ".join(screen.split())
        found: list[SessionEvent | Stderr | ClientBound] = (
            [Activity("")] if self._dialogs - shown else []
        )  # first: a later Blocked keeps the idle pause
        found += [Blocked(d) for d in self.run.dialogs if d in shown - self._dialogs]
        limit = self.run.limit(screen)
        if limit is not None and self._limit is None:
            found.append(limit)
        if shown - self._dialogs:
            self._mark(StatusState.BLOCKED, again=False)
        self._dialogs, self._limit = shown, limit
        self._track(found)
        return found

    def _shown(self, screen: str) -> frozenset[str]:
        return frozenset(d for d in self.run.dialogs if self.run.shows(d, screen))

    def _mark(self, word: StatusState, *, again: bool = True) -> None:
        """Its owner's alone (a view never marks); not again: only when its word differs."""
        if self._owned and (again or activity(self.name, self._generation) != word):
            try:
                self._store.set_activity(self._generation, word, self.prepared.spec.events)
            except (OSError, ValueError) as error:
                LOG.warning("tui session %s: mark %s: %s", self.name, word, error)

    def returncode(self, timeout: float) -> int | None:
        deadline = time.monotonic() + timeout
        while self._code is None:
            self._drain()
            self._code = self._terminal.returncode()
            if self._code is None and self._terminal.state() == Liveness.ABSENT:
                self._code = -signal.SIGHUP
            if self._code is None:
                if (left := deadline - time.monotonic()) <= 0:
                    return None
                time.sleep(min(SLICE, left))
        return self._code

    def state(self) -> Liveness:
        self._store.check()
        return self._terminal.state()

    def client_alive(self) -> bool:
        """Its client runs, its terminal probed afresh: state() may reuse a probe and stays LIVE
        while its ended client's terminal is kept.
        """
        self._store.check()
        try:
            return self._terminal.alive()
        except SessionError:  # it cannot tell
            return False

    def send(self, text: str) -> None:
        with self._store.sending():
            self._running()
            self._deliver(self.run.submit(text), prompted=True)

    def _deliver(self, actions: Sequence[Paste | Key], *, prompted: bool) -> None:
        """Failing after the first action: Unanswered. prompted: marks it begun, then submitted."""
        for index, action in enumerate(actions):
            try:
                self._terminal.send(action)
            except SessionError as error:
                if index == 0:
                    raise
                raise Unanswered(
                    f"send to tui session {self.name}: partly sent: {error}"
                ) from error
            if index == 0 and prompted:
                self._store.mark_prompted(self._generation)
        if prompted:
            self._store.mark_prompted(self._generation, submitted=True)

    def press(self, keys: Sequence[KeyPress]) -> None:
        with self._store.sending():
            self._running()
            self._terminal.press(keys)

    def screen(self, lines: int | None = None) -> str:
        self._running()
        try:
            return self._terminal.screen(lines)
        except NotShowable:
            raise SessionError(f"host {self.host.name} cannot show") from None

    def redisplay(self) -> bool:
        """Its owner handle's display opened again where gone; False: none to open."""
        return isinstance(self._terminal, ManagedProcess) and self._terminal.redisplay()

    def _running(self) -> None:
        self._store.check()
        if not self._terminal.alive():
            raise SessionError(f"tui session {self.name} is not running")

    def close(self) -> bool:
        return self._store.close(self._terminal if self._owned else None, self._generation)

    def stop(self) -> None:
        """Once, unless it raises; then ends (returncode: the code seen, else -SIGHUP)."""
        if self._live:
            self._store.stop(self._terminal, owned=self._owned, generation=self._generation)
            self._live = False
        self._end()
