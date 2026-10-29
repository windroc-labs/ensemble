"""The guard (stdlib only, run by path): every Host's managed worker runs it as the client's direct
parent (WDRC-199 FR-13 to FR-15), a Docker worker's as its container's main process (`--serve`).
"""

import argparse
import codecs
import contextlib
import ctypes
import errno
import fcntl
import json
import os
import re
import select
import signal
import socket
import struct
import subprocess
import sys
import termios
import time
import tty
import unicodedata
from collections import deque
from pathlib import Path

FRAME_MAX = 64 << 20
BUFFER_MAX = 8 << 20
HOOK_MAX = 1 << 20
READ_SIZE = 65536
TICK = 0.05
DISPLAY_GRACE = 2.0  # a client dead at once still reaches the display its Host starts at launch
UP = 5.0  # s its client runs after exec before the guard reports it up (`u`)
SIZE = (24, 80)  # rows, columns of a PTY no display sizes, without --size
SIZE_ARG = re.compile(r"([1-9][0-9]*)x([1-9][0-9]*)")
DELIVERY = re.compile(r"[0-9a-f]{32}")
GENERATION = re.compile(r"[0-9a-f]{32}")
RUN = "/run/ensemble/guard"  # a --serve guard's run dirs, one per generation
LAUNCH_WAIT = 60.0  # s from its start a --serve guard waits for its launch handoff
HEARTBEAT = 5.0  # s a --serve guard runs on with no byte from its harness
HOOK_ENV = "ENSEMBLE_GUARD_HOOK_SOCKET"


def frame(kind: bytes, payload: bytes) -> bytes:
    if len(kind) != 1 or len(payload) > FRAME_MAX:
        raise ValueError("invalid guard frame")
    return kind + len(payload).to_bytes(4, "big") + payload


def json_frame(kind: bytes, value: dict) -> bytes:
    return frame(kind, json.dumps(value, separators=(",", ":")).encode())


class Decoder:
    def __init__(self):
        self.pending = bytearray()

    def feed(self, data: bytes) -> list[tuple[bytes, bytes]]:
        self.pending.extend(data)
        found = []
        while len(self.pending) >= 5:
            size = int.from_bytes(self.pending[1:5], "big")
            if size > FRAME_MAX:
                raise ValueError("guard frame exceeds 64 MiB")
            if len(self.pending) < size + 5:
                break
            found.append((bytes(self.pending[:1]), bytes(self.pending[5 : size + 5])))
            del self.pending[: size + 5]
        return found


def hook_message(data: bytes) -> dict:
    value = json.loads(data)
    if (
        not isinstance(value, dict)
        or set(value) != {"label", "payload", "delivery_id"}
        or not isinstance(value["label"], str)
        or not value["label"]
        or len(value["label"]) > 256
        or not isinstance(value["payload"], dict)
        or not isinstance(value["delivery_id"], str)
        or not DELIVERY.fullmatch(value["delivery_id"])
    ):
        raise ValueError("invalid guard hook")
    return value


def launch(data: bytes) -> dict:
    """A launch handoff's fields; one fixed ValueError, never its content (env, credentials)."""
    try:
        value = json.loads(data)
    except (ValueError, RecursionError):
        value = None
    if not (
        isinstance(value, dict)
        and set(value) == {"argv", "env", "terminal", "generation", "size"}
        and isinstance(value["argv"], list)
        and value["argv"]
        and all(isinstance(arg, str) and "\0" not in arg for arg in value["argv"])
        and isinstance(value["env"], dict)
        and all(
            key
            and "=" not in key
            and "\0" not in key
            and isinstance(item, str)
            and "\0" not in item
            for key, item in value["env"].items()
        )
        and type(value["terminal"]) is bool
        and isinstance(value["generation"], str)
        and GENERATION.fullmatch(value["generation"])
        and isinstance(value["size"], str)
        and SIZE_ARG.fullmatch(value["size"])
        and all(int(n) < 1 << 16 for n in value["size"].split("x"))
    ):
        raise ValueError("invalid launch handoff")
    return value


def await_launch(deadline: float) -> tuple[dict, bytes]:
    """The launch handoff, stdin's first frame, and the bytes read after it."""
    decoder = Decoder()
    while (left := deadline - time.monotonic()) > 0:
        if not select.select([0], [], [], left)[0]:
            continue
        data = os.read(0, READ_SIZE)
        if not data:
            break
        try:
            found = decoder.feed(data)
        except ValueError:
            raise ValueError("invalid launch handoff") from None
        if found:
            if found[0][0] != b"l":
                raise ValueError("invalid launch handoff")
            rest = b"".join(frame(kind, payload) for kind, payload in found[1:])
            return launch(found[0][1]), rest + bytes(decoder.pending)
    raise ValueError("no launch handoff")


class Screen:
    """Bounded VT screen; visible cells and scrollback have separate ownership."""

    def __init__(self, limit=1 << 20, *, rows=SIZE[0], columns=SIZE[1]):
        self.limit, self.rows, self.columns = limit, rows, columns
        self.cells = [[" "] * columns for _ in range(rows)]
        self.history = deque(maxlen=max(1, limit // columns))
        self.row, self.column = 0, 0
        self.scroll_top, self.scroll_bottom = 0, rows - 1
        self.saved = (0, 0)
        self.normal: tuple[list[list[str]], int, int] | None = None
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.escape = ""

    def newline(self) -> None:
        if self.row == self.scroll_bottom:
            self._scroll(self.scroll_top, self.scroll_bottom, 1, history=True)
        else:
            self.row = min(self.rows - 1, self.row + 1)

    def _scroll(self, top: int, bottom: int, count: int, *, history=False) -> None:
        amount = min(abs(count), bottom - top + 1)
        blank = [[" "] * self.columns for _ in range(amount)]
        if count > 0:
            if history and top == 0 and self.normal is None:
                self.history.extend("".join(line).rstrip() for line in self.cells[:amount])
            self.cells[top : bottom + 1] = self.cells[top + amount : bottom + 1] + blank
        else:
            self.cells[top : bottom + 1] = blank + self.cells[top : bottom + 1 - amount]

    def csi(self, sequence: str) -> None:
        command, raw = sequence[-1], sequence[:-1]
        private = raw.startswith("?")
        try:
            values = [int(value or 0) for value in raw.lstrip("?").split(";")]
        except ValueError:
            return
        value = values[0] or 1
        if command in "Hf":
            self.row = min(self.rows - 1, max(0, value - 1))
            self.column = min(self.columns - 1, max(0, (values[1] if len(values) > 1 else 1) - 1))
        elif command in "ABCD":
            if command in "AB":
                self.row = max(
                    0, min(self.rows - 1, self.row + (value if command == "B" else -value))
                )
            else:
                self.column = max(
                    0, min(self.columns - 1, self.column + (value if command == "C" else -value))
                )
        elif command == "G":
            self.column = max(0, min(self.columns - 1, value - 1))
        elif command == "d":
            self.row = max(0, min(self.rows - 1, value - 1))
        elif command == "J":
            mode = values[0]
            for row in range(self.rows):
                if mode in (2, 3) or mode == 0 and row > self.row or mode == 1 and row < self.row:
                    self.cells[row] = [" "] * self.columns
            if mode in (0, 1):
                self.erase_line(mode)
            if mode == 3:
                self.history.clear()
        elif command == "K":
            self.erase_line(values[0])
        elif command == "P":
            line = self.cells[self.row]
            count = min(value, self.columns - self.column)
            line[self.column :] = line[self.column + count :] + [" "] * count
        elif command == "@":
            line = self.cells[self.row]
            line[self.column :] = ([" "] * min(value, self.columns) + line[self.column :])[
                : self.columns - self.column
            ]
        elif command == "r" and not private:
            top = value - 1
            bottom = (values[1] or self.rows) - 1 if len(values) > 1 else self.rows - 1
            if 0 <= top < bottom < self.rows or top == bottom == 0 and self.rows == 1:
                self.scroll_top, self.scroll_bottom = top, bottom
                self.row, self.column = 0, 0
        elif command in "ST":
            self._scroll(
                self.scroll_top,
                self.scroll_bottom,
                value if command == "S" else -value,
                history=True,
            )
        elif command in "LM" and self.scroll_top <= self.row <= self.scroll_bottom:
            self._scroll(self.row, self.scroll_bottom, value if command == "M" else -value)
            self.column = 0
        elif command == "s":
            self.saved = self.row, self.column
        elif command == "u":
            self.row, self.column = self.saved
        elif private and 1049 in values and command in "hl":
            if command == "h" and self.normal is None:
                self.normal = self.cells, self.row, self.column
                self.cells = [[" "] * self.columns for _ in range(self.rows)]
                self.row, self.column = 0, 0
            elif command == "l" and self.normal is not None:
                self.cells, self.row, self.column = self.normal
                self.normal = None

    def erase_line(self, mode: int) -> None:
        column = min(self.column, self.columns - 1)
        start = column if mode == 0 else 0
        end = column + 1 if mode == 1 else self.columns
        self.cells[self.row][start:end] = [" "] * (end - start)

    def append(self, data: bytes) -> None:
        for char in self.decoder.decode(data):
            if self.escape:
                self.escape += char
                if self.escape.startswith("\x1b[") and len(self.escape) > 2 and "@" <= char <= "~":
                    self.csi(self.escape[2:])
                    self.escape = ""
                elif self.escape[1] in "]P_^X":  # OSC, DCS, APC, PM, SOS: up to BEL or ST
                    if char == "\x07" or self.escape.endswith("\x1b\\"):
                        self.escape = ""
                    else:
                        self.escape = self.escape[:2] + char  # enough to see ST
                elif len(self.escape) == 2 and char not in "[]()":
                    if char == "7":
                        self.saved = self.row, self.column
                    elif char == "8":
                        self.row, self.column = self.saved
                    elif char in "DE":
                        if char == "E":
                            self.column = 0
                        self.newline()
                    elif char == "M":
                        if self.row == self.scroll_top:
                            self._scroll(self.scroll_top, self.scroll_bottom, -1)
                        else:
                            self.row = max(0, self.row - 1)
                    self.escape = ""
                elif self.escape.startswith(("\x1b(", "\x1b)")) and len(self.escape) == 3:
                    self.escape = ""
                if len(self.escape) > 256:
                    self.escape = ""
            elif char == "\x1b":
                self.escape = char
            elif char == "\r":
                self.column = 0
            elif char == "\n":
                self.newline()
            elif char == "\b":
                self.column = max(0, self.column - 1)
            elif char == "\t":
                self.column = min(self.columns - 1, (self.column // 8 + 1) * 8)
            elif char >= " " and char != "\x7f":
                if unicodedata.combining(char):
                    if self.column:
                        index = min(self.columns - 1, self.column - 1)
                        if self.cells[self.row][index] == "" and index:
                            index -= 1
                        if len(self.cells[self.row][index]) < 16:
                            self.cells[self.row][index] += char
                    continue
                width = min(
                    self.columns, 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
                )
                if self.column + width > self.columns:
                    self.column = 0
                    self.newline()
                self.cells[self.row][self.column] = char
                if width == 2 and self.column + 1 < self.columns:
                    self.cells[self.row][self.column + 1] = ""
                self.column += width

    def resize(self, rows: int, columns: int) -> None:
        if self.normal is None:
            for line in self.cells[: max(0, self.row + 1 - rows)]:
                self.history.append("".join(line).rstrip())
        else:
            cells, row, column = self.normal
            cells, row = fit(cells, row, rows, columns)
            self.normal = cells, row, min(column, columns)
        self.cells, self.row = fit(self.cells, self.row, rows, columns)
        self.rows, self.columns, self.column = rows, columns, min(self.column, columns)
        self.scroll_top, self.scroll_bottom = 0, rows - 1
        self.saved = min(self.saved[0], rows - 1), min(self.saved[1], columns - 1)

    def text(self, lines: int | None) -> str:
        rows = ["".join(row).rstrip() for row in self.cells]
        while rows and not rows[-1]:
            rows.pop()
        return "\n".join(rows if lines is None else [*self.history, *rows][-lines:])[-self.limit :]

    def snapshot(self) -> bytes:
        text = self.text(None).replace("\n", "\r\n")
        return f"\x1b[2J\x1b[H{text}\x1b[{self.row + 1};{min(self.column, self.columns - 1) + 1}H".encode()


def fit(cells: list[list[str]], row: int, rows: int, columns: int) -> tuple[list[list[str]], int]:
    """cells cut or padded to rows x columns, top lines dropped to keep row: (cells, its row)."""
    shift = max(0, row + 1 - rows)
    lines = [line[:columns] + [" "] * (columns - len(line)) for line in cells[shift : shift + rows]]
    return lines + [[" "] * columns for _ in range(rows - len(lines))], row - shift


def size(value: str) -> tuple[int, int]:
    """--size COLUMNSxROWS, each 1 to 65535, as (rows, columns)."""
    found = SIZE_ARG.fullmatch(value)
    if found is None or not all(int(n) < 1 << 16 for n in found.groups()):
        raise argparse.ArgumentTypeError(f"want COLUMNSxROWS, each 1 to 65535: {value!r}")
    return int(found[2]), int(found[1])


def window(data: bytes) -> tuple[int, int]:
    value = json.loads(data)
    if (
        not isinstance(value, dict)
        or set(value) != {"rows", "columns"}
        or any(type(value[key]) is not int or not 0 < value[key] < 1 << 16 for key in value)
    ):
        raise ValueError("invalid display size")
    return value["rows"], value["columns"]


def parse_stat(text: str) -> tuple[int, str, bool]:
    fields = text.rsplit(")", 1)[1].split()
    return int(fields[1]), fields[19], fields[0] == "Z"


def process_info(pid: int, library=None) -> tuple[int, str, bool] | None:
    if sys.platform == "linux":
        try:
            return parse_stat(Path(f"/proc/{pid}/stat").read_text())
        except (FileNotFoundError, ProcessLookupError):
            return None
    # Apple's proc_bsdinfo ABI: bsd/sys/proc_info.h (PROC_PIDTBSDINFO=3).
    if library is None:
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    data = ctypes.create_string_buffer(136)
    count = library.proc_pidinfo(pid, 3, 0, data, len(data))
    if count != len(data):
        if ctypes.get_errno() == errno.ESRCH:
            return None
        raise OSError(ctypes.get_errno(), "proc_pidinfo")
    status, parent = (
        struct.unpack_from("=I", data.raw, 4)[0],
        struct.unpack_from("=I", data.raw, 16)[0],
    )
    seconds, micros = struct.unpack_from("=QQ", data.raw, 120)
    return parent, f"{seconds}:{micros}", status == 5


def processes(required: dict[int, str] | None = None) -> dict[int, tuple[int, str, bool]]:
    library = None
    if sys.platform == "linux":
        pids = [int(name) for name in os.listdir("/proc") if name.isdecimal()]
    else:
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        count = library.proc_listpids(1, 0, None, 0)
        if count <= 0:
            raise OSError("proc_listpids failed")
        slots = (ctypes.c_int * (count // 4 + 1024))()
        size = library.proc_listpids(1, 0, slots, ctypes.sizeof(slots))
        if size <= 0 or size >= ctypes.sizeof(slots):
            raise OSError("proc_listpids incomplete")
        pids = [pid for pid in slots[: size // 4] if pid]
    rows = {}
    for pid in pids:
        try:
            if (info := process_info(pid, library)) is not None:
                rows[pid] = info
        except PermissionError:
            if required is not None and pid in required:
                raise
    return rows


def descendants(rows: dict, known: dict[int, str], parent: int) -> dict[int, str]:
    found = {pid: token for pid, token in known.items() if pid in rows and rows[pid][1] == token}
    while True:
        more = {
            pid: token for pid, (ppid, token, _) in rows.items() if ppid == parent or ppid in found
        }
        if not more.keys() - found.keys():
            return found
        found.update(more)


def subreaper() -> None:
    if sys.platform == "linux":
        if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "PR_SET_CHILD_SUBREAPER")


def close_signal(elapsed: float) -> signal.Signals:
    return signal.SIGKILL if elapsed >= 1.5 else signal.SIGTERM if elapsed >= 0.5 else signal.SIGHUP


def signal_process(pid: int, token: str, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError):
        if sys.platform == "linux":
            descriptor = os.pidfd_open(pid)
            try:
                info = process_info(pid)
                if info is not None and info[1] == token:
                    signal.pidfd_send_signal(descriptor, sig)
            finally:
                os.close(descriptor)
        elif (info := process_info(pid)) is not None and info[1] == token:
            os.kill(pid, sig)


def listener(path: str) -> socket.socket:
    server = socket.socket(socket.AF_UNIX)
    try:
        server.bind(path)
        os.chmod(path, 0o600)
        server.listen(16)
        server.settimeout(0)
    except BaseException:
        server.close()
        raise
    return server


def child_setup(slave: int | None) -> None:
    signal.pthread_sigmask(signal.SIG_SETMASK, ())
    if slave is not None:
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)


class Guard:
    def __init__(
        self,
        args,
        lease: int | None = None,
        *,
        env: dict[str, str] | None = None,
        heartbeat: float | None = None,
        pending: bytes = b"",
    ):
        self.args = args
        self.env, self.heartbeat, self.pending = env, heartbeat, pending
        self.heard = 0.0
        self.proc = None
        self.master = None
        self.streams = {}
        self.servers = {}
        self.peers = {}
        self.output = deque()
        self.output_size = 0
        self.taken = deque()  # (peer, delivery) of each written h, until its harness's k
        self.input = bytearray()
        self.input_end = False  # a headless client's stdin closes once input is written
        self.input_closed = False
        self.decoder = Decoder()
        self.unsized = args.size  # the PTY's (rows, columns) while no display sizes it
        self.screen = Screen(rows=self.unsized[0], columns=self.unsized[1])
        self.size = self.unsized
        self.sizes = {}  # display peer: its terminal's (rows, columns), the latest last
        self.known = {}
        self.closing = None
        self.signalled = {}
        self.channel = True
        self.exit_sent = False
        self.exit_at = 0.0
        self.spawned = 0.0
        self.up_sent = False
        self.displayed = False
        self.children_pending = False
        self.lease = os.environ.get("ENSEMBLE_GUARD_LEASE_FD") if lease is None else lease
        self.directory = os.path.dirname(args.hook_socket)

    def state(self, status: str) -> None:
        if self.lease is None:
            return
        value = {"generation": os.path.basename(self.directory), "state": status}
        path = os.path.join(self.directory, "state.json")
        descriptor = os.open(
            path + ".new", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(path + ".new", path)

    def start(self) -> None:
        subreaper()
        self.servers[listener(self.args.hook_socket)] = "hook"
        if self.args.display_socket:
            self.servers[listener(self.args.display_socket)] = "display"
        slave = None
        try:
            if self.args.terminal:
                self.master, slave = os.openpty()
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", *self.size, 0, 0))
            env = None
            if self.env is not None or slave is not None:
                env = {**os.environ, **(self.env or {})}
                # A Docker client inherits no TERM; without one it draws no color.
                if slave is not None:
                    env.setdefault("TERM", "xterm-256color")
            self.proc = subprocess.Popen(
                self.args.command,
                stdin=slave if slave is not None else subprocess.PIPE,
                stdout=slave if slave is not None else subprocess.PIPE,
                stderr=slave if slave is not None else subprocess.PIPE,
                start_new_session=True,
                close_fds=True,
                preexec_fn=lambda: child_setup(slave),
                env=env,
            )
            self.spawned = time.monotonic()
        finally:
            if slave is not None:
                os.close(slave)
        if self.master is not None:
            self.streams[self.master] = b"t"
            self.input_fd = self.master
        else:
            assert (
                self.proc.stdout is not None
                and self.proc.stderr is not None
                and self.proc.stdin is not None
            )
            self.streams[self.proc.stdout.fileno()] = b"o"
            self.streams[self.proc.stderr.fileno()] = b"e"
            self.input_fd = self.proc.stdin.fileno()
        for descriptor in (*self.streams, self.input_fd, 0, 1):
            os.set_blocking(descriptor, False)
        self.known = descendants(processes(), {}, os.getpid())
        self.state("running")
        self.emit(json_frame(b"s", {"pid": self.proc.pid}))

    def emit(self, wire: bytes, ack: socket.socket | None = None, delivery: str = "") -> None:
        if self.output_size + len(wire) > BUFFER_MAX:
            raise ValueError("guard output buffer full")
        self.output.append((wire, ack, delivery))
        self.output_size += len(wire)

    def close(self) -> None:
        if self.closing is None:
            self.closing = time.monotonic()
            while self.taken:
                self.ack(*self.taken.popleft())

    def drop(self, peer) -> None:
        self.peers.pop(peer, None)
        peer.close()
        if self.sizes.pop(peer, None) is not None and self.closing is None:
            self.follow()

    def follow(self) -> None:
        """The PTY at the latest sized display's size, else unsized; a change redraws its client."""
        size = next(reversed(self.sizes.values()), self.unsized)
        if self.master is not None and size != self.size:
            fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", *size, 0, 0))
            self.screen.resize(*size)
            self.size = size

    def flush(self) -> None:
        while self.output and self.channel:
            wire, peer, delivery = self.output[0]
            try:
                count = os.write(1, wire)
            except BlockingIOError:
                break
            except BrokenPipeError:
                self.channel = False
                self.output.clear()
                self.output_size = 0
                self.close()
                break
            self.output_size -= count
            if count < len(wire):
                self.output[0] = (wire[count:], peer, delivery)
                break
            self.output.popleft()
            if peer is not None and self.closing is not None:  # stdin no longer read
                self.ack(peer, delivery)
            elif peer is not None:
                self.taken.append((peer, delivery))

    def ack(self, peer, delivery: str) -> None:
        with contextlib.suppress(OSError):
            peer.sendall(json.dumps({"ok": True, "delivery_id": delivery}).encode() + b"\n")
        self.drop(peer)

    def command(self, kind: bytes, data: bytes) -> None:
        if kind == b"i":
            if not data:
                self.input_end = self.input_end or self.master is None
            elif not self.input_end:
                if len(self.input) + len(data) > BUFFER_MAX:
                    raise ValueError("guard input buffer full")
                self.input.extend(data)
        elif kind == b"c" and json.loads(data) == {}:
            self.close()
        elif kind == b"p" and json.loads(data) == {}:
            pass
        elif kind == b"k" and json.loads(data) == {}:
            if self.taken:
                self.ack(*self.taken.popleft())
        elif kind == b"q" and self.master is not None:
            request = json.loads(data)
            lines = request.get("lines")
            if lines is not None and (type(lines) is not int or lines < 1):
                raise ValueError("invalid screen line count")
            self.emit(json_frame(b"v", {"text": self.screen.text(lines)}))
        else:
            raise ValueError("invalid guard command")

    def read_channel(self) -> None:
        """Harness bytes: each read restarts the heartbeat limit; EOF is a close."""
        data = os.read(0, READ_SIZE)
        if not data:
            self.close()
            return
        self.heard = time.monotonic()
        for kind, payload in self.decoder.feed(data):
            self.command(kind, payload)

    def listen(self, ready) -> None:
        """After a select, so a guard frozen past the limit (docker pause) reads its bytes first."""
        if 0 not in ready and self.lost(time.monotonic()):
            self.close()

    def lost(self, now: float) -> bool:
        return (
            self.heartbeat is not None
            and self.closing is None
            and now - self.heard > self.heartbeat
        )

    def write_input(self, *, ended: bool) -> None:
        """Queued input to the client while it runs, never after; at end of input its stdin closes
        once all of it is written.
        """
        if ended or self.input_closed:
            return
        if self.input:
            with contextlib.suppress(BlockingIOError, BrokenPipeError):
                count = os.write(self.input_fd, self.input)
                del self.input[:count]
        if self.input_end and not self.input:
            assert self.proc is not None and self.proc.stdin is not None
            self.input_closed = True
            self.proc.stdin.close()

    def peer_read(self, peer) -> None:
        kind, pending = self.peers[peer]
        try:
            data = peer.recv(READ_SIZE)
        except OSError:
            self.drop(peer)
            return
        if not data:
            self.drop(peer)
        elif kind == "display":
            try:
                for what, payload in pending.feed(data):
                    self.display_frame(peer, what, payload)
            except (ValueError, UnicodeError):
                self.drop(peer)
                return
            if len(pending.pending) > BUFFER_MAX:
                self.drop(peer)
        else:
            pending.extend(data)
            if len(pending) > HOOK_MAX:
                self.drop(peer)
            elif b"\n" in pending:
                try:
                    message = hook_message(bytes(pending).split(b"\n", 1)[0])
                except (ValueError, UnicodeError):
                    self.drop(peer)
                    return
                self.peers[peer] = ("ack", bytearray())
                self.emit(json_frame(b"h", message), peer, message["delivery_id"])

    def display_frame(self, peer, kind: bytes, data: bytes) -> None:
        if kind == b"i" and len(self.input) + len(data) <= BUFFER_MAX:
            self.input.extend(data)
        elif kind == b"w":
            size = window(data)
            self.sizes.pop(peer, None)
            self.sizes[peer] = size
            self.follow()
        else:
            raise ValueError("invalid display frame")

    def read_stream(self, descriptor: int) -> None:
        try:
            data = os.read(descriptor, READ_SIZE)
        except OSError as error:
            if error.errno != errno.EIO:
                raise
            data = b""
        if not data:
            self.streams.pop(descriptor)
            return
        kind = self.streams[descriptor]
        if kind == b"t":
            self.screen.append(data)
            for peer, (role, _) in list(self.peers.items()):
                if role == "display":
                    try:
                        if peer.send(data) != len(data):
                            self.drop(peer)
                    except OSError:
                        self.drop(peer)
        elif self.channel:
            self.emit(frame(kind, data))

    def reap(self) -> None:
        assert self.proc is not None
        self.proc.poll()
        self.children_pending = False
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    self.children_pending = True
                    break
                if pid == self.proc.pid:
                    self.proc.returncode = os.waitstatus_to_exitcode(status)
            except ChildProcessError:
                break

    def tick(self) -> bool:
        assert self.proc is not None
        rows = processes(self.known)
        self.known = descendants(rows, self.known, os.getpid())
        now = time.monotonic()  # before the reap: an exit before UP s can never be reported up
        self.reap()
        if self.proc.returncode is not None:
            self.close()
        elif not self.up_sent and self.channel and now - self.spawned >= UP:
            self.emit(json_frame(b"u", {}))
            self.up_sent = True
        if self.closing is None:
            return False
        elapsed = time.monotonic() - self.closing
        sig = close_signal(elapsed)
        for pid, token in self.known.items():
            if not rows[pid][2] and self.signalled.get((pid, token)) != sig:
                signal_process(pid, token, sig)
                self.signalled[pid, token] = sig
        live = any(not rows[pid][2] for pid in self.known)
        if elapsed > 8 and (live or self.children_pending):
            raise OSError("worker tree exit unconfirmed")
        return self.proc.returncode is not None and not live and not self.children_pending

    def run(self) -> int:
        try:
            self.start()
            assert self.proc is not None
            self.heard = time.monotonic()
            for kind, payload in self.decoder.feed(self.pending):
                self.command(kind, payload)
            ended = False
            scan_due = 0.0
            while True:
                now = time.monotonic()
                if now >= scan_due:
                    ended = self.tick() or ended
                    scan_due = now + 0.1
                if ended and not self.streams and not self.exit_sent:
                    self.final_hooks()
                    self.state("exited")
                    if self.channel:
                        self.emit(json_frame(b"x", {"returncode": self.proc.returncode}))
                    self.exit_sent, self.exit_at = True, time.monotonic()
                self.flush()
                if self.exit_sent and (not self.output or not self.channel):
                    if not self.lingers(time.monotonic()):
                        return 0
                    for server, role in self.servers.items():
                        if role == "display":
                            self.take_display(server)
                # A stalled consumer must not hold an already-reclaimed guard indefinitely.
                if ended and self.closing is not None and time.monotonic() - self.closing > 9:
                    return 1
                if self.exit_sent:
                    time.sleep(TICK)
                    continue
                readers: list[int | socket.socket] = [0] if self.closing is None else []
                if self.output_size < BUFFER_MAX - READ_SIZE - HOOK_MAX:
                    readers += list(self.streams)
                    readers += [p for p, (role, _) in self.peers.items() if role != "ack"]
                    readers += list(self.servers) if len(self.peers) < 64 else []
                ready, _, _ = select.select(readers, [], [], TICK)
                self.listen(ready)
                for descriptor in ready:
                    if descriptor == 0:
                        self.read_channel()
                    elif not isinstance(descriptor, int) and descriptor in self.servers:
                        if self.servers[descriptor] == "display":
                            self.take_display(descriptor)
                        else:
                            peer, _ = descriptor.accept()
                            peer.settimeout(0)
                            self.peers[peer] = (self.servers[descriptor], bytearray())
                    elif descriptor in self.peers:
                        self.peer_read(descriptor)
                    elif isinstance(descriptor, int) and descriptor in self.streams:
                        self.read_stream(descriptor)
                self.write_input(ended=ended)
        finally:
            if self.proc is not None and not self.exit_sent:
                self.close()
                with contextlib.suppress(OSError):
                    while not self.tick():
                        time.sleep(TICK)
            for peer in list(self.peers):
                self.drop(peer)
            for server in self.servers:
                server.close()
            if self.master is not None:
                os.close(self.master)
            if self.proc is not None:
                for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                    if stream is not None:
                        stream.close()

    def lingers(self, now: float) -> bool:
        """A --display-grace terminal guard whose client ended before any display came waits
        DISPLAY_GRACE s.
        """
        return (
            self.args.display_grace
            and self.master is not None
            and not self.displayed
            and now - self.exit_at < DISPLAY_GRACE
        )

    def take_display(self, server) -> None:
        """A display connecting: the current screen first, then live output."""
        try:
            peer, _ = server.accept()
        except BlockingIOError:
            return
        peer.settimeout(0)
        self.peers[peer] = ("display", Decoder())
        self.displayed = True
        snapshot = self.screen.snapshot()
        try:
            if peer.send(snapshot) != len(snapshot):
                self.drop(peer)
        except OSError:
            self.drop(peer)

    def final_hooks(self) -> None:
        for server, role in self.servers.items():
            if role == "hook":
                while len(self.peers) < 64:
                    try:
                        peer, _ = server.accept()
                    except BlockingIOError:
                        break
                    peer.settimeout(0)
                    self.peers[peer] = ("hook", bytearray())
        for peer, (role, _) in list(self.peers.items()):
            if role == "hook":
                while peer in self.peers and self.peers[peer][0] == "hook":
                    self.peer_read(peer)


def attach(path: str) -> int:
    peer = socket.socket(socket.AF_UNIX)
    try:
        peer.connect(path)
    except OSError as error:
        peer.close()
        print(f"guard: display {path}: {error.strerror or error}", file=sys.stderr)
        return 1
    previous = termios.tcgetattr(0) if os.isatty(0) else None
    resized, poke = os.pipe()  # SIGWINCH wakes the select through it
    os.set_blocking(poke, False)
    handler = signal.signal(signal.SIGWINCH, lambda _sig, _frame: None)
    wakeup = signal.set_wakeup_fd(poke)

    def send_size() -> None:
        if previous is not None:
            columns, rows = os.get_terminal_size(0)
            peer.sendall(json_frame(b"w", {"rows": rows, "columns": columns}))

    try:
        if previous is not None:
            tty.setraw(0, termios.TCSANOW)  # TCSAFLUSH would drop what was typed before it
        send_size()
        while True:
            ready, _, _ = select.select([0, peer, resized], [], [])
            for descriptor in ready:
                if descriptor == resized:
                    os.read(resized, READ_SIZE)
                    send_size()
                    continue
                data = os.read(0, READ_SIZE) if descriptor == 0 else peer.recv(READ_SIZE)
                if not data:
                    return 0
                if descriptor == 0:
                    peer.sendall(frame(b"i", data))
                else:
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
    finally:
        signal.set_wakeup_fd(wakeup)
        signal.signal(signal.SIGWINCH, handler)
        os.close(resized)
        os.close(poke)
        if previous is not None:
            termios.tcsetattr(0, termios.TCSADRAIN, previous)
        peer.close()


def hold_lease(path: str) -> int | None:
    """PATH flocked for this process's life (its dir made 0700); None while another holds it."""
    os.makedirs(os.path.dirname(path), 0o700, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return None
    return descriptor


def probe(directory: str) -> str:
    """live: a guard holds DIR/lease; exited: free, its state.json confirming the exit; absent: no
    DIR; dead: anything else.
    """
    if not os.path.lexists(directory):
        return "absent"
    try:
        descriptor = os.open(os.path.join(directory, "lease"), os.O_RDWR | os.O_NOFOLLOW)
    except OSError:
        return "dead"
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "live"
        with open(os.path.join(directory, "state.json"), encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, ValueError):
        return "dead"
    else:
        expected = {"generation": os.path.basename(directory), "state": "exited"}
        return "exited" if value == expected else "dead"
    finally:
        os.close(descriptor)


def arguments() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attach")
    parser.add_argument("--terminal", action="store_true")
    parser.add_argument("--hook-socket")
    parser.add_argument("--display-socket")
    parser.add_argument("--display-grace", action="store_true")
    parser.add_argument("--size", type=size, default=SIZE)
    parser.add_argument("--probe")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = arguments()
    args = parser.parse_args(argv)
    if args.attach:
        return attach(args.attach)
    if args.probe:
        print(probe(args.probe))
        return 0
    if args.serve:
        if (
            args.command
            or args.hook_socket
            or args.display_socket
            or args.terminal
            or args.size != SIZE
        ):
            parser.error("--serve takes no other option")
        return serve()
    if args.command[:1] == ["--"]:
        args.command.pop(0)
    if not args.command or not args.hook_socket:
        parser.error("--hook-socket and a client command are required")
    return supervise(args, None)


def serve() -> int:
    """A Docker worker container's main process: its client as the launch handoff says."""
    try:
        found, pending = await_launch(time.monotonic() + LAUNCH_WAIT)
    except (OSError, ValueError) as error:
        print(f"guard: {error}", file=sys.stderr)
        return 1
    args, lease, options = launched(found, pending)
    return supervise(args, lease, **options)


def run_paths(generation: str) -> dict[str, str]:
    run = f"{RUN}/{generation}"
    return {"dir": run, "hook": f"{run}/hook", "lease": f"{run}/lease", "display": f"{run}/display"}


def launched(found: dict, pending: bytes) -> tuple[argparse.Namespace, str, dict]:
    """A handoff's guard args, lease path and Guard options."""
    paths = run_paths(found["generation"])
    args = argparse.Namespace(
        terminal=found["terminal"],
        hook_socket=paths["hook"],
        display_socket=paths["display"] if found["terminal"] else None,
        display_grace=False,
        size=size(found["size"]),
        command=found["argv"],
    )
    env = {**found["env"], HOOK_ENV: args.hook_socket}
    return args, paths["lease"], {"env": env, "heartbeat": HEARTBEAT, "pending": pending}


def supervise(args, lease_path: str | None, **options) -> int:
    signal.pthread_sigmask(signal.SIG_SETMASK, ())
    # A handled disposition is reset by exec; clients must not inherit ignored HUP.
    lease = None
    if lease_path:
        try:
            lease = hold_lease(lease_path)
        except OSError as error:
            print(f"guard: {lease_path}: {error.strerror}", file=sys.stderr)
            return 1
        if lease is None:
            print("guard: lease held", file=sys.stderr)
            return 1
    runtime = Guard(args, lease, **options)
    for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda _sig, _frame: runtime.close())
    try:
        return runtime.run()
    except (OSError, ValueError) as error:
        print(f"guard: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
