"""The guard channel's host end, shared by every Host's managed worker: frames in, input,
screen queries and close out.
"""

import json
import os
import select
import subprocess
import threading
import time
from collections import deque

from ensemble.session.engine.types import (
    ConfigError,
    Key,
    KeyPress,
    Liveness,
    NotShowable,
    Paste,
    SessionError,
    Unanswered,
    WorkerExit,
    WorkerHook,
    WorkerOutput,
)
from ensemble.session.harness import guard
from ensemble.session.hosts.base import GUARD_WAIT, InteractiveProcess

QUEUE_MAX = 8 << 20

KEYS = {
    "Enter": b"\r",
    "Escape": b"\x1b",
    "Tab": b"\t",
    "BTab": b"\x1b[Z",
    "BSpace": b"\x7f",
    "Up": b"\x1b[A",
    "Down": b"\x1b[B",
    "Right": b"\x1b[C",
    "Left": b"\x1b[D",
    "Home": b"\x1b[H",
    "End": b"\x1b[F",
    "PageUp": b"\x1b[5~",
    "PageDown": b"\x1b[6~",
    "F1": b"\x1bOP",
    "F2": b"\x1bOQ",
    "F3": b"\x1bOR",
    "F4": b"\x1bOS",
    **{f"F{i}": f"\x1b[{n}~".encode() for i, n in enumerate((15, 17, 18, 19, 20, 21, 23, 24), 5)},
}


def key_bytes(key: KeyPress) -> bytes:
    if len(key.key) == 1:
        text = key.key.upper() if key.shift else key.key
        if key.ctrl and text in (" ", "/", "?"):
            value = {" ": b"\x00", "/": b"\x1f", "?": b"\x7f"}[text]
        else:
            value = (
                bytes([ord(text.upper()) & 31])
                if key.ctrl and "@" <= text.upper() <= "_"
                else text.encode()
            )
    elif key.key in ("Enter", "Escape", "Tab", "BSpace"):
        value = KEYS[key.key]
        if key.key == "Tab" and key.shift:
            value = KEYS["BTab"]
        elif key.key == "BSpace" and key.ctrl:
            value = b"\x08"
    else:
        value = KEYS[key.key]
        modifier = 1 + key.shift + 2 * key.meta + 4 * key.ctrl
        if modifier > 1:
            value = (
                b"\x1b[1;" + str(modifier).encode() + value[-1:]
                if value[-1:] != b"~"
                else value[:-1] + b";" + str(modifier).encode() + b"~"
            )
            return value
    return (b"\x1b" if key.meta else b"") + value


class ManagedProcess(InteractiveProcess):
    def __init__(self, name, generation, proc, *, terminal, start_reader=True):
        self.name, self.generation, self.proc = name, generation, proc
        self.terminal = terminal
        self.condition = threading.Condition()
        self.sending = threading.Lock()
        self.query = threading.Lock()
        self.messages = deque()
        self.size = 0
        self.code = None
        self.last_lines: str | None = None  # its terminal's, from its exit frame
        self.failure = None
        self.started = False
        self.up = False
        self.view = None
        self.view_pending = False
        self.reader = None
        self.exit_thread: threading.Thread | None = None
        self.releases = 0
        self.releaser: threading.Thread | None = None
        self.closing = False  # its stdin about to close: the releaser stops
        self.input_ended = False
        if start_reader:
            self.reader = threading.Thread(target=self.pump, name=f"guard-{name}", daemon=True)
            self.reader.start()

    def fail(self, reason: str) -> None:
        with self.condition:
            if self.failure is None:
                self.failure = reason
                self.messages.clear()
                self.size = 0
                self.proc.stdin.close()
                self.condition.notify_all()
                self._ended()

    def _ended(self) -> None:
        """Under the condition: on_exit, once, on a thread of its own."""
        if self.exit_thread is None:
            self.exit_thread = threading.Thread(
                target=self.on_exit, name=f"guard-{self.name}-end", daemon=True
            )
            self.exit_thread.start()

    def on_exit(self) -> None:
        """After the exit frame or a channel failure, once."""

    def accept_hook(self, data: bytes) -> WorkerHook | None:
        """An h frame's hook; None: dropped. Raises ValueError for the channel's failure."""
        value = guard.hook_message(data)
        return WorkerHook(value["label"], value["payload"], value["delivery_id"])

    def accept(self, kind: bytes, data: bytes) -> None:
        with self.condition:
            if self.failure is not None:
                return
            if self.code is not None:
                raise ValueError("guard frame after exit")
            message = None
            if kind == b"s" and not self.started:
                value = json.loads(data)
                if (
                    not isinstance(value, dict)
                    or type(value.get("pid")) is not int
                    or value["pid"] <= 0
                ):
                    raise ValueError("invalid guard start")
                self.started = True
            elif not self.started:
                raise ValueError("guard did not confirm exec")
            elif kind == b"u":
                if self.up or json.loads(data) != {}:
                    raise ValueError("invalid guard up")
                self.up = True
            elif kind in (b"o", b"e"):
                message = WorkerOutput(data, "stdout" if kind == b"o" else "stderr")
            elif kind == b"h":
                message = self.accept_hook(data)
            elif kind == b"v":
                value = json.loads(data)
                if not isinstance(value, dict) or not isinstance(value.get("text"), str):
                    raise ValueError("invalid guard view")
                self.view = value["text"]
                self.view_pending = False
            elif kind == b"x":
                value = json.loads(data)
                if (
                    not isinstance(value, dict)
                    or type(value.get("returncode")) is not int
                    or not isinstance(value.get("text", ""), str)
                ):
                    raise ValueError("invalid guard exit")
                self.code = value["returncode"]
                if "text" in value:
                    self.last_lines = value["text"][-guard.DIAGNOSTIC_CHARS :]
                message = WorkerExit(self.code)
                self._ended()
            else:
                raise ValueError("invalid guard frame")
            if message is not None:
                if self.size + len(data) + 5 > QUEUE_MAX:
                    self.fail("guard consumer buffer exceeded 8 MiB; worker closed")
                    return
                self.messages.append((message, len(data) + 5))
                self.size += len(data) + 5
            self.condition.notify_all()

    def pump(self) -> None:
        decoder = guard.Decoder()
        try:
            while data := self.proc.stdout.read(guard.READ_SIZE):
                if self.failure is not None:
                    continue  # Always drain after overflow or protocol failure.
                try:
                    for kind, payload in decoder.feed(data):
                        self.accept(kind, payload)
                        if kind == b"h":
                            self.release_hook()
                except (ValueError, UnicodeError, TypeError, RecursionError) as error:
                    self.fail(str(error))
            if self.code is None or decoder.pending:
                self.fail("guard channel ended without confirmed worker exit")
        except OSError as error:
            self.fail(f"guard channel: {error}")
        finally:
            self.proc.stdout.close()
            try:
                self.proc.wait(timeout=GUARD_WAIT)
            except subprocess.TimeoutExpired:
                self.fail("guard transport exit unconfirmed")
            with self.condition:
                self.condition.notify_all()

    def checked(self) -> None:
        if self.failure is not None:
            raise Unanswered(f"worker {self.name}: {self.failure}")

    def await_start(self) -> None:
        with self.condition:
            self.condition.wait_for(lambda: self.started or self.failure is not None, GUARD_WAIT)
            self.checked()
            if not self.started:
                self.fail("guard exec confirmation timed out")
                self.checked()

    def read(self, timeout: float):
        with self.condition:
            self.condition.wait_for(
                lambda: self.messages or self.failure is not None or self.code is not None,
                max(0, timeout),
            )
            self.checked()
            found = [message for message, _ in self.messages]
            self.messages.clear()
            self.size = 0
            return found

    def release_hook(self) -> None:
        with self.condition:
            self.releases += 1
            if self.releaser is None:
                self.releaser = threading.Thread(
                    target=self._release, name=f"guard-{self.name}-release", daemon=True
                )
                self.releaser.start()
            self.condition.notify_all()

    def _release(self) -> None:
        """Until its exit frame, a failure or closing: then the guard acks hooks itself."""
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.releases or self._released())
                if self._released():
                    return
                count, self.releases = self.releases, 0
            try:
                for _ in range(count):
                    self.send_frame(b"k", b"{}")
            except SessionError:
                return

    def _released(self) -> bool:
        return self.closing or self.failure is not None or self.code is not None

    def end_releases(self) -> None:
        """Before its stdin closes: the releaser's last k written, none after."""
        with self.condition:
            self.closing = True
            self.condition.notify_all()
            releaser = self.releaser
        self._join(releaser)

    def send_frame(self, kind: bytes, data: bytes) -> None:
        self.checked()
        wire = memoryview(guard.frame(kind, data))
        with self.sending:
            deadline = time.monotonic() + GUARD_WAIT
            while wire:
                self.checked()
                left = deadline - time.monotonic()
                if left <= 0:
                    self.fail("guard input timed out")
                    self.checked()
                try:
                    if select.select([], [self.proc.stdin], [], max(0, left))[1]:
                        count = os.write(self.proc.stdin.fileno(), wire)
                        wire = wire[count:]
                except BlockingIOError:
                    continue
                except BrokenPipeError as error:
                    self.input_closed(error)
                except (OSError, ValueError) as error:
                    self.fail(f"guard input: {error}")
                    self.checked()

    def input_closed(self, error: OSError) -> None:
        """Its guard is gone: once the reader has its confirmed exit, input is merely refused;
        else the channel fails closed.
        """
        with self.condition:
            self.condition.wait_for(
                lambda: self.code is not None or self.failure is not None, GUARD_WAIT
            )
            if self.code is not None and self.failure is None:
                raise SessionError(f"worker {self.name}: exited") from error
        self.fail(f"guard input: {error}")
        self.checked()

    def write(self, data: bytes) -> None:
        with self.condition:
            self.checked()
            if self.code is not None:
                raise SessionError(f"worker {self.name}: exited")
            if self.input_ended:
                raise SessionError(f"worker {self.name}: input ended")
            if not data and self.terminal:
                return
            self.input_ended = not data
        self.send_frame(b"i", data)

    def require_terminal(self) -> None:
        if not self.terminal:
            raise NotShowable(f"worker {self.name}: headless")

    def send(self, action: Paste | Key) -> None:
        self.require_terminal()
        self.write(
            b"\x1b[200~" + action.text.encode() + b"\x1b[201~"
            if isinstance(action, Paste)
            else key_bytes(KeyPress.parse(action.name))
        )

    def press(self, keys) -> None:
        self.require_terminal()
        self.write(b"".join(key_bytes(key) for key in keys))

    def screen(self, lines: int | None = None) -> str:
        self.require_terminal()
        if lines is not None and (type(lines) is not int or lines < 1):
            raise ConfigError("lines: want a positive integer")
        request = json.dumps({"lines": lines}).encode()
        with self.query:
            with self.condition:
                self.condition.wait_for(
                    lambda: (
                        not self.view_pending or self.failure is not None or self.code is not None
                    ),
                    GUARD_WAIT,
                )
                self.checked()
                if self.code is not None:
                    raise SessionError(f"worker {self.name}: exited")
                if self.view_pending:
                    raise Unanswered(f"worker {self.name}: screen unanswered")
                self.view = None
                self.view_pending = True
            self.send_frame(b"q", request)
            with self.condition:
                self.condition.wait_for(
                    lambda: (
                        self.view is not None or self.failure is not None or self.code is not None
                    ),
                    GUARD_WAIT,
                )
                self.checked()
                if self.view is None:
                    if self.code is not None:
                        raise SessionError(f"worker {self.name}: exited")
                    raise Unanswered(f"worker {self.name}: screen unanswered")
                return self.view

    def state(self) -> Liveness:
        if self.failure is not None:
            return Liveness.UNKNOWN
        return Liveness.ABSENT if self.code is not None else Liveness.LIVE

    def returncode(self) -> int | None:
        self.checked()
        return self.code

    def close(self) -> None:
        self.checked()
        if self.code is None:
            try:
                self.send_frame(b"c", b"{}")
            except SessionError:
                if self.code is None:
                    raise  # else its exit came first: nothing left to end
            with self.condition:
                self.condition.wait_for(
                    lambda: self.code is not None or self.failure is not None, GUARD_WAIT
                )
                self.checked()
                if self.code is None:
                    self.fail("worker exit unconfirmed")
                    self.checked()
        self.end_releases()
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=GUARD_WAIT)
        except subprocess.TimeoutExpired as error:
            raise Unanswered(f"worker {self.name}: guard exit unconfirmed") from error
        self._join(self.reader)
        self.end_releases()
        self.checked()

    def _join(self, thread: threading.Thread | None) -> None:
        if thread is not None:
            thread.join(timeout=GUARD_WAIT)
            if thread.is_alive():
                raise Unanswered(f"worker {self.name}: channel still open")
