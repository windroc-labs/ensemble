"""The container's hook relay (stdlib only, run by path; src/broker/protocol/docker-relay/protocol.schema.json). `serve GEN`
queues each stub request under the queue dir and sends the queue one message at a time over the
uplink socket; `connect` joins the supervisor's exec stream to that socket.
"""

import contextlib
import json
import os
import re
import select
import socket
import sys
import threading
import time

RUN = "/run/ensemble"
SOCKET = f"{RUN}/relay.sock"
UPLINK = f"{RUN}/relay-up.sock"
LINE_MAX = 90 << 20
PAYLOAD_MAX = 64 << 20
WAIT = 10
BLOCK = 1 << 16
LABEL = re.compile(r"[A-Za-z0-9_.-]{1,64}")
GEN = re.compile(r"[0-9a-f]{32}")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
BASE64 = re.compile(r"[A-Za-z0-9+/]*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?")
MESSAGE = re.compile(r"([1-9][0-9]*)\.json")


def _unique(pairs):
    found = dict(pairs)
    if len(found) != len(pairs):
        raise ValueError("duplicate key")
    return found


def _constant(name):
    raise ValueError(name)


def _object(line: bytes):
    if not line.endswith(b"\n"):
        return None
    try:
        found = json.loads(line, object_pairs_hook=_unique, parse_constant=_constant)
    except (ValueError, RecursionError):
        return None
    return found if isinstance(found, dict) else None


def _int(value, least: int) -> bool:
    return type(value) is int and value >= least


def _gen(value) -> bool:
    return isinstance(value, str) and GEN.fullmatch(value) is not None


def _argv(value) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 3
        and value[0] == "hook"
        and isinstance(value[1], str)
        and value[1].startswith("/")
        and not CONTROL.search(value[1])
        and isinstance(value[2], str)
        and LABEL.fullmatch(value[2]) is not None
    )


def _payload(value) -> bool:
    return (
        isinstance(value, str)
        and len(value) % 4 == 0
        and len(value) // 4 * 3 - value[-2:].count("=") <= PAYLOAD_MAX
        and BASE64.fullmatch(value) is not None
    )


def stub_request(line: bytes) -> dict | None:
    found = _object(line)
    if not (
        found is not None
        and found.keys() == {"id", "argv", "payload"}
        and _int(found["id"], 0)
        and _argv(found["argv"])
        and _payload(found["payload"])
    ):
        return None
    return found


def relay_request(line: bytes) -> dict | None:
    found = _object(line)
    if not (
        found is not None
        and found.keys() == {"id", "gen", "seq", "argv", "payload"}
        and _int(found["id"], 0)
        and _gen(found["gen"])
        and _int(found["seq"], 1)
        and _argv(found["argv"])
        and _payload(found["payload"])
    ):
        return None
    return found


def relay_reply(line: bytes) -> dict | None:
    found = _object(line)
    if found is None or type(found.get("ok")) is not bool:
        return None
    if found == {"ok": False} or (
        (
            found.keys() == {"gen", "seq", "ok"}
            or (
                found.keys() == {"gen", "seq", "ok", "retry"}
                and found["ok"] is False
                and found["retry"] is True
            )
        )
        and _gen(found["gen"])
        and _int(found["seq"], 1)
    ):
        return found
    return None


def _line(found: dict) -> bytes:
    return json.dumps(found, separators=(",", ":")).encode() + b"\n"


class Queue:
    """Message `<seq>.json` files and the last seq given in `counter`, under `path`."""

    def __init__(self, path: str) -> None:
        self.path, self.changed = path, threading.Condition()
        os.makedirs(path, mode=0o700, exist_ok=True)
        self.last = max([self._counter(), *self._seqs()])

    def _counter(self) -> int:
        try:
            with open(os.path.join(self.path, "counter"), encoding="ascii") as f:
                return int(f.read())
        except (OSError, ValueError):
            return 0

    def _seqs(self) -> list[int]:
        return sorted(int(m[1]) for n in os.listdir(self.path) if (m := MESSAGE.fullmatch(n)))

    def _write(self, name: str, data: bytes) -> None:
        temp = os.path.join(self.path, f".{name}.tmp")
        with open(temp, "wb") as f:
            f.write(data)
        os.replace(temp, os.path.join(self.path, name))

    def put(self, gen: str, request: dict) -> int:
        with self.changed:
            seq = self.last + 1
            message = {"id": request["id"], "gen": gen, "seq": seq}
            message.update(argv=request["argv"], payload=request["payload"])
            self._write(f"{seq}.json", _line(message))
            self.last = seq
            self.changed.notify_all()
            self._write("counter", str(seq).encode())
        return seq

    def first(self, wait: float | None = None) -> tuple[int, bytes] | None:
        """The smallest undeleted message; None if none came within `wait` s (None: no limit)."""
        with self.changed:
            if not (seqs := self.changed.wait_for(self._seqs, wait)):
                return None
            seq = seqs[0]
            with open(os.path.join(self.path, f"{seq}.json"), "rb") as f:
                return seq, f.read()

    def drop(self, seq: int) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(os.path.join(self.path, f"{seq}.json"))


def answer(line: bytes, queue: Queue, gen: str) -> dict:
    request = stub_request(line)
    if request is None:
        return {"queued": False}
    try:
        queue.put(gen, request)
    except OSError:
        return {"id": request["id"], "queued": False}
    return {"id": request["id"], "queued": True}


def pump(queue: Queue, reader, writer, idle: float | None = None) -> None:
    """Stop-and-wait: retry:true retains the first message and retries after WAIT s; other matching
    replies drop it. Returns at EOF, an invalid/mismatched reply, or `idle` s with nothing to send.
    """
    while (found := queue.first(idle)) is not None:
        seq, message = found
        sent = relay_request(message)
        if sent is None:
            queue.drop(seq)
            continue
        writer.write(message)
        writer.flush()
        reply = relay_reply(reader.readline(LINE_MAX + 1))
        if reply is None or ("gen" in reply and (reply["gen"], reply["seq"]) != (sent["gen"], seq)):
            return
        if reply.get("retry"):
            time.sleep(WAIT)
            continue
        queue.drop(seq)


class Daemon:
    def __init__(self, gen: str, run: str = RUN) -> None:
        self.gen, self.run = gen, run
        self.socket, self.uplink = (
            os.path.join(run, os.path.basename(p)) for p in (SOCKET, UPLINK)
        )
        self.listeners: list[socket.socket] = []

    def start(self) -> bool:
        """False, binding nothing, when a daemon answers on its socket."""
        with (
            socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe,
            contextlib.suppress(OSError),
        ):
            probe.connect(self.socket)
            return False
        self.queue = Queue(f"{self.run}/relay-queue")
        uplink, stubs = self._listen(self.uplink), self._listen(self.socket)
        threading.Thread(target=self._uplinks, args=(uplink,), daemon=True).start()
        threading.Thread(target=self._stubs, args=(stubs,), daemon=True).start()
        return True

    def close(self) -> None:
        for listener in self.listeners:
            with contextlib.suppress(OSError):
                listener.shutdown(socket.SHUT_RDWR)  # Linux: close alone leaves accept() running
            listener.close()

    def _listen(self, path: str) -> socket.socket:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(path)
        listener.listen()
        self.listeners.append(listener)
        return listener

    def _uplinks(self, listener: socket.socket) -> None:
        with contextlib.suppress(OSError):
            while True:
                conn, _ = listener.accept()
                with (
                    conn,
                    contextlib.suppress(OSError, ValueError),
                    conn.makefile("rb") as reader,
                    conn.makefile("wb") as writer,
                ):
                    pump(self.queue, reader, writer)

    def _stubs(self, listener: socket.socket) -> None:
        with contextlib.suppress(OSError):
            while True:
                conn, _ = listener.accept()
                threading.Thread(target=self._stub, args=(conn,), daemon=True).start()

    def _stub(self, conn: socket.socket) -> None:
        with conn, contextlib.suppress(OSError):
            conn.settimeout(WAIT)
            with conn.makefile("rb") as reader:
                line = reader.readline(LINE_MAX + 1)
            conn.sendall(_line(answer(line, self.queue, self.gen)))


def connect(path: str = UPLINK, stdin: int = 0, stdout: int = 1, wait: float = WAIT) -> int:
    deadline = time.monotonic() + wait
    while True:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            conn.connect(path)
            break
        except OSError as error:
            conn.close()
            if time.monotonic() >= deadline:
                print(f"ensemble relay connect: {error}", file=sys.stderr)
                return 1
            time.sleep(0.2)
    with conn, contextlib.suppress(OSError):
        while True:
            ready = select.select([stdin, conn], [], [])[0]
            if stdin in ready:
                data = os.read(stdin, BLOCK)
                if not data:
                    break
                conn.sendall(data)
            if conn in ready:
                data = conn.recv(BLOCK)
                if not data:
                    break
                while data:
                    data = data[os.write(stdout, data) :]
    return 0


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) == 2 and args[0] == "serve" and GEN.fullmatch(args[1]):
        daemon = Daemon(args[1])
        if daemon.start():
            threading.Event().wait()
        return 0
    if args == ["connect"]:
        return connect()
    print("usage: relay serve GENERATION | relay connect", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
