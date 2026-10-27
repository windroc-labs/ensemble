"""The guard channel's host end, shared by every Host's managed worker: frames in, input,
screen queries and close out (docs/sessions.md › Guard protocol).
"""

import json
import os
import select
import subprocess
import sys
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
    Unanswered,
    WorkerExit,
    WorkerHook,
    WorkerOutput,
)
from ensemble.session.harness import guard
from ensemble.session.hosts.base import InteractiveProcess

QUEUE_MAX = 8 << 20
WAIT = 10

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
        if key.ctrl and text in (" ", "?"):
            value = b"\x00" if text == " " else b"\x7f"
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
        self.failure = None
        self.started = False
        self.view = None
        self.view_pending = False
        self.reader = None
        if start_reader:
            self.reader = threading.Thread(target=self.pump, name=f"guard-{name}", daemon=True)
            self.reader.start()

    def fail(self, reason: str) -> None:
        with self.condition:
            if self.failure is None:
                self.failure = reason
                self.messages.clear()
                self.size = 0
                # Unbuffered FileIO.close never waits for a consumer or a buffered flush.
                self.proc.stdin.close()
                self.condition.notify_all()

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
            elif kind in (b"o", b"e"):
                message = WorkerOutput(data, "stdout" if kind == b"o" else "stderr")
            elif kind == b"h":
                value = guard.hook_message(data)
                message = WorkerHook(value["label"], value["payload"], value["delivery_id"])
            elif kind == b"v":
                value = json.loads(data)
                if not isinstance(value, dict) or not isinstance(value.get("text"), str):
                    raise ValueError("invalid guard view")
                self.view = value["text"]
                self.view_pending = False
            elif kind == b"x":
                value = json.loads(data)
                if not isinstance(value, dict) or type(value.get("returncode")) is not int:
                    raise ValueError("invalid guard exit")
                self.code = value["returncode"]
                message = WorkerExit(self.code)
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
                except (ValueError, UnicodeError, TypeError) as error:
                    self.fail(str(error))
            if self.code is None or decoder.pending:
                self.fail("guard channel ended without confirmed worker exit")
        except OSError as error:
            self.fail(f"guard channel: {error}")
        finally:
            self.proc.stdout.close()
            try:
                self.proc.wait(timeout=WAIT)
            except subprocess.TimeoutExpired:
                self.fail("guard transport exit unconfirmed")
            with self.condition:
                self.condition.notify_all()
            if self.failure is not None and sys.platform == "darwin":
                self.close_display()

    def checked(self) -> None:
        if self.failure is not None:
            raise Unanswered(f"worker {self.name}: {self.failure}")

    def await_start(self) -> None:
        with self.condition:
            self.condition.wait_for(lambda: self.started or self.failure is not None, WAIT)
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

    def send_frame(self, kind: bytes, data: bytes) -> None:
        self.checked()
        wire = memoryview(guard.frame(kind, data))
        with self.sending:
            deadline = time.monotonic() + WAIT
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
                except (OSError, ValueError) as error:
                    self.fail(f"guard input: {error}")
                    self.checked()

    def write(self, data: bytes) -> None:
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
                # A timed-out query still owns the next reply on this ordered channel.
                self.condition.wait_for(
                    lambda: not self.view_pending or self.failure is not None, WAIT
                )
                self.checked()
                if self.view_pending:
                    raise Unanswered(f"worker {self.name}: screen unanswered")
                self.view = None
                self.view_pending = True
            self.send_frame(b"q", request)
            with self.condition:
                self.condition.wait_for(
                    lambda: self.view is not None or self.failure is not None, WAIT
                )
                self.checked()
                if self.view is None:
                    raise Unanswered(f"worker {self.name}: screen unanswered")
                return self.view

    def state(self) -> Liveness:
        if self.failure is not None:
            return Liveness.UNKNOWN
        return Liveness.ABSENT if self.code is not None else Liveness.LIVE

    def returncode(self) -> int | None:
        self.checked()
        return self.code

    def close_display(self) -> None:
        """Ends this generation's display, where its Host keeps one."""

    def close(self) -> None:
        self.checked()
        if self.code is None:
            self.send_frame(b"c", b"{}")
            with self.condition:
                self.condition.wait_for(
                    lambda: self.code is not None or self.failure is not None, WAIT
                )
                self.checked()
                if self.code is None:
                    self.fail("worker exit unconfirmed")
                    self.checked()
        try:
            self.proc.wait(timeout=WAIT)
        except subprocess.TimeoutExpired as error:
            raise Unanswered(f"worker {self.name}: guard exit unconfirmed") from error
        if self.reader is not None:
            self.reader.join(timeout=WAIT)
            if self.reader.is_alive():
                raise Unanswered(f"worker {self.name}: channel still open")
        self.checked()
        self.proc.stdin.close()
        self.close_display()

    # These legacy members disappear in WDRC-211 after all callers migrate.
    def alive(self) -> bool:
        return self.state() == Liveness.LIVE

    def wait_exit(self, timeout: float) -> bool:
        with self.condition:
            self.condition.wait_for(
                lambda: self.code is not None or self.failure is not None, timeout
            )
            return self.state() == Liveness.ABSENT

    def kill(self) -> None:
        self.close()

    def record(self):
        return None

    def display(self):
        return None
