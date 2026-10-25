"""FakeHost runs programs under a generation-scoped ProcessControl; supervisor.py writes session
state and events. Metadata lives under settings["root"] (default ~/.ensemble/fake-host); a handle
keeps its launch's controller even when the session name is reused.
"""

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

from ensemble.session import ConfigError, Host, Process, SessionError
from ensemble.session.engine.status_log import SessionStatusLog
from ensemble.session.engine.types import (
    MARKS,
    Key,
    KeyPress,
    Launch,
    Liveness,
    NamedKey,
    Paste,
    StatusState,
)
from ensemble.session.hosts.base import InteractiveProcess
from ensemble.utils import Display, ProcessControl, Record

from session.tests.case import ROOTS
from utils.tests.sandbox import clean_env

NAME = re.compile(r"[A-Za-z0-9_-]+\Z")
KEYS = {"Enter": "\n", "Escape": "\x1b", "Tab": "\t"}
XTERM = {
    NamedKey.ENTER: b"\r",
    NamedKey.ESCAPE: b"\x1b",
    NamedKey.TAB: b"\t",
    NamedKey.BTAB: b"\x1b[Z",
    NamedKey.BSPACE: b"\x7f",
    NamedKey.UP: b"\x1b[A",
    NamedKey.DOWN: b"\x1b[B",
    NamedKey.RIGHT: b"\x1b[C",
    NamedKey.LEFT: b"\x1b[D",
    NamedKey.HOME: b"\x1b[H",
    NamedKey.END: b"\x1b[F",
    NamedKey.PAGE_UP: b"\x1b[5~",
    NamedKey.PAGE_DOWN: b"\x1b[6~",
    NamedKey.F1: b"\x1bOP",
    NamedKey.F2: b"\x1bOQ",
    NamedKey.F3: b"\x1bOR",
    NamedKey.F4: b"\x1bOS",
    NamedKey.F5: b"\x1b[15~",
    NamedKey.F6: b"\x1b[17~",
    NamedKey.F7: b"\x1b[18~",
    NamedKey.F8: b"\x1b[19~",
    NamedKey.F9: b"\x1b[20~",
    NamedKey.F10: b"\x1b[21~",
    NamedKey.F11: b"\x1b[23~",
    NamedKey.F12: b"\x1b[24~",
}
SUPERVISOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "supervisor.py")
START_TIMEOUT = 10
POLL = 0.05


def _read(folder: str, file: str) -> str | None:
    try:
        with open(os.path.join(folder, file), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _meta(folder: str) -> dict:
    try:
        return json.loads(_read(folder, "meta.json") or "{}")
    except ValueError:
        return {}


class FakeProcess(Process):
    def __init__(self, name: str, folder: str, *, meta: dict | None = None) -> None:
        self.name = name
        self.folder = folder
        meta = _meta(folder) if meta is None else meta
        self._id = meta.get("id")
        control = meta.get("control")
        self._control = ProcessControl(control) if control else None

    def _own(self) -> dict:
        meta = _meta(self.folder)
        return meta if self._id is not None and meta.get("id") == self._id else {}

    def alive(self) -> bool:
        return bool(self._own()) and _read(self.folder, "rc") is None and self._live_group()

    @property
    def identity(self) -> str:
        return self._id

    def state(self) -> Liveness:
        return Liveness.LIVE if self._own() else Liveness.ABSENT

    def returncode(self) -> int | None:
        if not self._own():
            return -signal.SIGHUP
        value = _read(self.folder, "rc")
        return None if value is None else int(value)

    def activity(self) -> StatusState | None:
        value = _read(self.folder, "activity") if self._own() else None
        return StatusState(value) if value in MARKS else None

    def mark(self, word: StatusState, events: str | None) -> None:
        if self._own():
            with open(os.path.join(self.folder, "activity"), "w", encoding="utf-8") as stream:
                stream.write(word)
            if events:
                SessionStatusLog(events).write(self.name, word)

    def wait_exit(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while self.alive() or self._live_group():
            if time.monotonic() >= deadline:
                return False
            time.sleep(POLL)
        return True

    def kill(self) -> None:
        if self._control is not None:
            try:
                self._control.kill()
            except OSError as error:
                raise SessionError(f"process group {self.name}: {error}") from error
        if not self.wait_exit(5):
            raise SessionError(f"process group {self.name}: did not exit")

    def close(self) -> None:
        self.kill()
        if self._own():
            shutil.rmtree(self.folder, ignore_errors=True)

    def record(self) -> Record | None:
        line = self._own().get("record")
        return None if line is None else Record.parse(line)

    def publish(self, record: Record) -> None:
        meta = self._own()
        if not meta:
            raise SessionError(f"{self.name}: gone")
        temporary = os.path.join(self.folder, "meta.json.tmp")
        with open(temporary, "w", encoding="utf-8") as f:
            json.dump({**meta, "record": record.to_line()}, f)
        os.replace(temporary, os.path.join(self.folder, "meta.json"))

    def session_identity(self):
        metadata = self._own().get("metadata", {})
        return metadata.get("sid"), metadata.get("generation")

    def display(self) -> Display:
        return self._own().get("display")

    def _live_group(self) -> bool:
        if self._control is None:
            return False
        try:
            return self._control.live()
        except OSError as error:
            raise SessionError(f"process group {self.name}: {error}") from error


class FakeTerminal(FakeProcess, InteractiveProcess):
    def send(self, action: Paste | Key) -> None:
        if isinstance(action, Paste):
            data = action.text.encode()
        elif isinstance(action, Key) and action.name in KEYS:
            data = KEYS[action.name].encode()
        else:
            raise SessionError(f"{self.name}: cannot send {action!r}")
        self._write(data)

    def press(self, keys) -> None:
        self._write(b"".join(_xterm(key) for key in keys))

    def _write(self, data: bytes) -> None:
        if not self.alive():
            raise SessionError(f"{self.name}: not alive")
        try:
            fd = os.open(os.path.join(self.folder, "input"), os.O_WRONLY | os.O_NONBLOCK)
            try:
                while data:
                    data = data[os.write(fd, data) :]
            finally:
                os.close(fd)
        except OSError as e:
            raise SessionError(f"{self.name}: cannot send: {e}") from None


def _xterm(key: KeyPress) -> bytes:
    """xterm's sequences: Ctrl only on an ASCII letter, Shift only on Tab (BTab); else
    ConfigError.
    """
    if not isinstance(key, KeyPress):
        raise ConfigError(f"key {key!r:.80}: want a KeyPress")
    name = key.key
    if key.shift:
        if name != NamedKey.TAB:
            raise ConfigError(f"key {key}: the fake host cannot translate it")
        name = NamedKey.BTAB
    if key.ctrl:
        if not (len(name) == 1 and name.isascii() and name.isalpha()):
            raise ConfigError(f"key {key}: the fake host cannot translate it")
        data = bytes([ord(name.lower()) & 0x1F])
    else:
        data = XTERM.get(name, name.encode())
    return b"\x1b" + data if key.meta else data


KINDS = {"detach": FakeProcess, "terminal": FakeTerminal}


class FakeHost(Host):
    def root(self) -> str:
        return os.path.expanduser(self.settings.get("root", "~/.ensemble/fake-host"))

    def detach(self, name, launch: Launch, *, display=None, events=None, record=None):
        return self._start(
            "detach",
            name,
            launch,
            display=display,
            events=events,
            record=record,
            metadata={} if record is None or record.sid is None else {"sid": record.sid},
        )

    def terminal(
        self, name, launch: Launch, *, display=None, events=None, record=None, metadata=None
    ):
        return self._start(
            "terminal",
            name,
            launch,
            display=display,
            events=events,
            record=record,
            metadata=metadata,
        )

    def attach(self, name: str) -> FakeProcess:
        folder = os.path.join(self.root(), name)
        cls = KINDS.get(_meta(folder).get("kind")) if NAME.fullmatch(name) else None
        if cls is None:
            raise SessionError(f"no {self.name} session {name}")
        return cls(name, folder)

    def list(self) -> list[str]:
        root = self.root()
        names = sorted(os.listdir(root)) if os.path.isdir(root) else []
        return [
            name
            for name in names
            if NAME.fullmatch(name) and _meta(os.path.join(root, name)).get("kind") in KINDS
        ]

    def _start(self, kind, name, launch: Launch, *, display, events, record, metadata=None):
        argv, cwd, env = launch.argv, launch.cwd, launch.env
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise ValueError(f"name: want [A-Za-z0-9_-]+, not {name!r}")
        if not (isinstance(argv, list | tuple) and argv and all(isinstance(a, str) for a in argv)):
            raise ValueError(f"argv: want a non-empty list of str, not {argv!r}")
        if not isinstance(cwd, str) or not os.path.isdir(cwd):
            raise ValueError(f"cwd: want a directory, not {cwd!r}")
        folder = os.path.join(self.root(), name)
        previous = FakeProcess(name, folder)
        if previous.alive():
            raise SessionError(f"{self.name} session {name} is live")
        if os.path.isdir(folder):
            previous.close()
        shutil.rmtree(folder, ignore_errors=True)
        os.makedirs(folder, mode=0o700)
        if kind == "terminal":  # FR-47: no terminal of its own, so none of the caller's identity
            env = {k: v for k, v in env.items() if k not in ("TMUX", "TMUX_PANE", "TERM")}
        control = ProcessControl.prepare(argv, cwd, env=env)
        meta = {
            "control": control.path,
            "kind": kind,
            "id": uuid.uuid4().hex,
            "argv": list(argv),
            "cwd": cwd,
            "display": display,
        }
        meta["metadata"] = dict(metadata or {})
        meta["record"] = None if record is None else record.to_line()
        for file, data in (("meta.json", meta), ("env.json", dict(env))):
            with open(os.path.join(folder, file), "w", encoding="utf-8") as f:
                json.dump(data, f)
        events = os.path.abspath(events) if events else ""
        own = clean_env(None, *ROOTS)
        with open(os.path.join(folder, "output"), "ab") as output:
            supervisor = subprocess.Popen(
                [sys.executable, "-P", "-S", SUPERVISOR, folder, events],
                env=own,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=output,
                start_new_session=True,
            )
        threading.Thread(target=supervisor.wait, daemon=True).start()
        try:
            deadline = time.monotonic() + START_TIMEOUT
            while _read(folder, "pid") is None and not os.path.isfile(
                os.path.join(control.path, "state.json")
            ):
                if supervisor.returncode is not None or time.monotonic() > deadline:
                    tail = (_read(folder, "output") or "").strip().rpartition("\n")[2]
                    raise SessionError(f"{self.name} session {name} did not start: {tail}")
                time.sleep(POLL)
            control.wait_started()
        except BaseException:
            with contextlib.suppress(OSError):
                control.cancel()
            if supervisor.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(supervisor.pid, signal.SIGKILL)
            raise
        return KINDS[kind](name, folder, meta=meta)
