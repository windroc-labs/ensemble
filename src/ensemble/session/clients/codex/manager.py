"""A manager TUI's private connection to the shared server. Standalone, stdlib only."""

import contextlib
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Mapping
from typing import BinaryIO

THREAD_METHODS = frozenset({"thread/start", "thread/resume", "thread/fork"})
NONINTERACTIVE = frozenset(
    {
        "exec",
        "e",
        "review",
        "login",
        "logout",
        "plugin",
        "plugins",
        "mcp",
        "mcp-server",
        "app-server",
        "exec-server",
        "completion",
        "sandbox",
        "debug",
        "doctor",
        "update",
        "agents",
        "queue",
        "app",
        "remote-control",
        "apply",
        "archive",
        "unarchive",
        "delete",
    }
)
VALUED = frozenset(
    {
        "--add-dir",
        "--ask-for-approval",
        "--cd",
        "--config",
        "--disable",
        "--enable",
        "--image",
        "--local-provider",
        "--model",
        "--profile",
        "--remote",
        "--remote-auth-token-env",
        "--sandbox",
        "-C",
        "-a",
        "-c",
        "-i",
        "-m",
        "-p",
        "-s",
    }
)
MAX_MESSAGE = 128 << 20
REMOTE_VALUES = frozenset({"--model", "-m", "--cd", "-C", "--image", "-i"})
REMOTE_FLAGS = frozenset({"--last", "--all", "--include-non-interactive", "--no-alt-screen"})
ALIASES = {
    "-C": "--cd",
    "-a": "--ask-for-approval",
    "-c": "--config",
    "-i": "--image",
    "-m": "--model",
    "-p": "--profile",
    "-s": "--sandbox",
    "-h": "--help",
    "-V": "--version",
}


class Arguments:
    def __init__(self, argv: tuple[str, ...]):
        self.options: dict[str, str | None] = {}
        self.positionals: list[str] = []
        self.command: str | None = None
        literal = False
        words = iter(argv)
        for word in words:
            if not literal and word == "--":
                literal = True
            elif not literal and word.startswith("--"):
                name, equals, value = word.partition("=")
                self.options[name] = (
                    value if equals else next(words, None) if name in VALUED else None
                )
            elif not literal and word.startswith("-") and word != "-":
                for at, letter in enumerate(word[1:], 2):
                    name = "-" + letter
                    value = None
                    if name in VALUED:
                        value = word[at:] or next(words, None)
                    self.options[ALIASES.get(name, name)] = value
                    if name in VALUED:
                        break
            else:
                if not self.positionals and not literal:
                    self.command = word
                self.positionals.append(word)

    def interactive(self) -> bool:
        return (
            not self.options.keys() & {"--help", "--version"} and self.command not in NONINTERACTIVE
        )

    def local_launch(self) -> bool:
        """Remote mode discards local permission choices; preserve native resolution."""
        return bool(self.options.keys() - REMOTE_VALUES - REMOTE_FLAGS) or any(
            name in os.environ
            for name in ("CODEX_EXEC_SERVER_URL", "OPENAI_WORKLOAD_IDENTITY_CONTEXT")
        )


class ThreadBridge:
    def __init__(self, environ: Mapping[str, str], record: Callable[[dict], None]):
        self.environ = dict(environ)
        self.record = record
        self.pending: set[str | int] = set()

    def request(self, payload: bytes) -> bytes:
        try:
            message = json.loads(payload)
        except ValueError:
            return payload
        if not isinstance(message, dict) or message.get("method") not in THREAD_METHODS:
            return payload
        params = message.setdefault("params", {})
        if params.get("config") is None:
            params["config"] = {}
        config = params["config"]
        policy = config.setdefault("shell_environment_policy", {})
        policy.setdefault("set", {}).update(self.environ)
        self.pending.add(message["id"])
        return json.dumps(message).encode()

    def response(self, payload: bytes) -> bytes:
        try:
            message = json.loads(payload)
        except ValueError:
            return payload
        if not isinstance(message, dict) or "method" in message:
            return payload
        key = message.get("id")
        if key not in self.pending:
            return payload
        self.pending.remove(key)
        if "error" not in message:
            thread = message["result"]["thread"]
            sid, cwd = thread["id"], thread["cwd"]
            if (
                not isinstance(sid, str)
                or not sid
                or not isinstance(cwd, str)
                or not cwd.startswith("/")
            ):
                raise ValueError("invalid thread identity")
            self.record({"session_id": sid, "cwd": cwd})
        return payload


def exact(stream: BinaryIO, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            raise EOFError
        data.extend(chunk)
    return bytes(data)


def read_frame(stream: BinaryIO) -> tuple[bool, int, bytes]:
    first, second = exact(stream, 2)
    if first & 0x70:
        raise ValueError("unexpected websocket extension")
    size = second & 127
    if size in (126, 127):
        size = int.from_bytes(exact(stream, 2 if size == 126 else 8), "big")
    if size > MAX_MESSAGE:
        raise ValueError("websocket frame too large")
    mask = exact(stream, 4) if second & 128 else b""
    payload = exact(stream, size)
    if mask:
        payload = bytes(value ^ mask[at % 4] for at, value in enumerate(payload))
    return bool(first & 128), first & 15, payload


def frame(payload: bytes, *, masked: bool, opcode: int = 1, final: bool = True) -> bytes:
    size = len(payload)
    length = (
        bytes([size])
        if size < 126
        else b"\x7e" + size.to_bytes(2, "big")
        if size < 65536
        else b"\x7f" + size.to_bytes(8, "big")
    )
    mask = os.urandom(4) if masked else b""
    header = (
        bytes([opcode | (128 if final else 0), length[0] | (128 if masked else 0)]) + length[1:]
    )
    return (
        header
        + mask
        + (bytes(value ^ mask[at % 4] for at, value in enumerate(payload)) if mask else payload)
    )


def relay(source: socket.socket, target: socket.socket, transform, *, masked: bool) -> None:
    with source.makefile("rb") as stream:
        chunks: list[bytes] = []
        kind = 0
        size = 0
        while True:
            final, opcode, payload = read_frame(stream)
            if opcode >= 8:
                target.sendall(frame(payload, masked=masked, opcode=opcode))
                if opcode == 8:
                    return
                continue
            if opcode != 0:
                if chunks:
                    raise ValueError("interleaved websocket messages")
                kind = opcode
            elif not chunks:
                raise ValueError("unexpected continuation")
            chunks.append(payload)
            size += len(payload)
            if size > MAX_MESSAGE:
                raise ValueError("websocket message too large")
            if final:
                data = b"".join(chunks)
                transformed = transform(data) if kind == 1 else data
                if transformed != data:
                    limit = max(map(len, chunks)) or 1
                    chunks = [
                        transformed[at : at + limit] for at in range(0, len(transformed), limit)
                    ]
                for at, piece in enumerate(chunks):
                    target.sendall(
                        frame(
                            piece,
                            masked=masked,
                            opcode=kind if at == 0 else 0,
                            final=at == len(chunks) - 1,
                        )
                    )
                chunks, size = [], 0


def headers(connection: socket.socket) -> bytes:
    data = bytearray()
    while not data.endswith(b"\r\n\r\n"):
        chunk = connection.recv(1)
        if not chunk:
            raise EOFError
        data.extend(chunk)
        if len(data) > 65536:
            raise ValueError("websocket handshake too large")
    return bytes(data)


def plain_handshake(data: bytes) -> bytes:
    removed = {b"sec-websocket-extensions", b"x-codex-app-server-compression"}
    return b"\r\n".join(
        line for line in data.split(b"\r\n") if line.partition(b":")[0].lower() not in removed
    )


def parent(pid: int) -> int | None:
    try:
        if sys.platform.startswith("linux"):
            with open(f"/proc/{pid}/stat", "rb") as f:
                return int(f.read().rpartition(b")")[2].split()[1])
        done = subprocess.run(
            ("ps", "-p", str(pid), "-o", "ppid="), capture_output=True, timeout=2, check=False
        )
        return int(done.stdout) if done.returncode == 0 else None
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def authorized(pid: int, uid: int, own_uid: int, *, launch: int, hold: int, get_parent) -> bool:
    if uid != own_uid:
        return False
    ancestors = {pid}
    while (pid := get_parent(pid) or 0) > 1 and pid not in ancestors:
        ancestors.add(pid)
    return launch in ancestors and hold in ancestors


def peer(connection: socket.socket) -> tuple[int, int]:
    if sys.platform.startswith("linux"):
        pid, uid, _gid = struct.unpack(
            "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )
        return pid, uid
    credentials = connection.getsockopt(0, 1, 76)
    uid = struct.unpack_from("I", credentials, 4)[0]
    pid = struct.unpack("i", connection.getsockopt(0, 2, 4))[0]
    return pid, uid


def recording(command: tuple[str, ...], pid: int, event: dict) -> None:
    done = subprocess.run(
        (*command, "--binding", "record", "--client", "codex", "--client-pid", str(pid)),
        input=json.dumps(event),
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    if done.returncode != 0 or not done.stdout or json.loads(done.stdout) != {"recorded": True}:
        raise ValueError("manager refused the client session binding")


def serve(
    listener: socket.socket, upstream: str, launch: int, hold: int, recorder: tuple[str, ...]
) -> None:
    listener.settimeout(1)
    while True:
        try:
            client, _address = listener.accept()
            break
        except TimeoutError:
            os.kill(launch, 0)
    with client:
        pid, uid = peer(client)
        if not authorized(pid, uid, os.getuid(), launch=launch, hold=hold, get_parent=parent):
            raise ValueError("unauthorized manager client")
        listener.close()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            client.settimeout(10)
            request = plain_handshake(headers(client))
            server.settimeout(10)
            server.connect(upstream)
            server.sendall(request)
            response = headers(server)
            client.sendall(response)
            if not response.startswith(b"HTTP/1.1 101 "):
                return
            client.settimeout(None)
            server.settimeout(None)
            environ = {
                key: value
                for key, value in os.environ.items()
                if key.startswith("ENSEMBLE_HOLDER_") or key == "ENSEMBLE_MANAGER"
            }
            bridge = ThreadBridge(environ, lambda event: recording(recorder, pid, event))

            def forward():
                try:
                    relay(client, server, bridge.request, masked=True)
                except (OSError, EOFError, ValueError, KeyError, TypeError):
                    pass
                finally:
                    with contextlib.suppress(OSError):
                        server.shutdown(socket.SHUT_RDWR)

            worker = threading.Thread(target=forward)
            worker.start()
            try:
                relay(server, client, bridge.response, masked=False)
            finally:
                with contextlib.suppress(OSError):
                    client.shutdown(socket.SHUT_RDWR)
                worker.join()


def executable(root: str, launcher: str | None) -> str:
    excluded = {
        os.path.realpath(os.path.join(root, "skills", "manage", "scripts", "codex")),
        os.path.realpath(os.path.expanduser("~/.ensemble/bin/codex")),
    }
    if launcher:
        excluded.add(os.path.realpath(launcher))
    for directory in os.environ.get("PATH", os.defpath).split(os.pathsep):
        candidate = os.path.abspath(os.path.join(directory, "codex"))
        if (
            os.path.realpath(candidate) not in excluded
            and os.path.isfile(candidate)
            and os.access(candidate, os.X_OK)
        ):
            return candidate
    raise ValueError("no Codex executable on PATH beyond the Ensemble launcher")


def main(root: str, argv: tuple[str, ...]) -> int:
    launcher = None
    if argv[:1] == ("--ensemble-launcher",):
        launcher, argv = argv[1], argv[2:]
    real = executable(root, launcher)
    arguments = Arguments(argv)
    if (
        "ENSEMBLE_AGENT_SESSION" in os.environ
        or not os.environ.get("ENSEMBLE_MANAGER")
        or not arguments.interactive()
    ):
        os.execv(real, (real, *argv))
    recorder = (sys.executable, "-I", os.path.join(root, "src", "ensemble", "manager", "rearm.py"))
    context = subprocess.run(
        (*recorder, "--binding", "context"), capture_output=True, text=True, timeout=5, check=False
    )
    if context.returncode != 0 or not context.stdout:
        os.execv(real, (real, *argv))
    identity = json.loads(context.stdout)
    if "--remote" in arguments.options:
        raise ValueError("manager clients require the local shared server; omit --remote")
    if arguments.local_launch():
        os.execv(real, (real, "--no-daemon", *argv))
    started = subprocess.run(
        (real, "app-server", "daemon", "start"),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if started.returncode != 0:
        raise ValueError("cannot start the shared Codex server")
    upstream = json.loads(started.stdout)["socketPath"]
    directory = tempfile.mkdtemp(prefix="ensemble-cx-", dir="/tmp")
    path = os.path.join(directory, "rpc")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(path)
        os.chmod(path, 0o600)
        listener.listen(1)
        launch = os.getpid()
        proxy = os.fork()
        if proxy == 0:

            def stop(_signum, _frame):
                raise InterruptedError

            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, stop)
            try:
                serve(listener, upstream, launch, identity["holder_pid"], recorder)
            except (EOFError, InterruptedError, BrokenPipeError, ConnectionResetError):
                pass
            except (
                OSError,
                ValueError,
                KeyError,
                TypeError,
                subprocess.SubprocessError,
            ) as error:
                print(f"codex: manager connection ended: {error}", file=sys.stderr)
            finally:
                listener.close()
                shutil.rmtree(directory)
            os._exit(0)
        listener.close()
        if "--cd" not in arguments.options:
            argv = ("--cd", os.getcwd(), *argv)
        os.execv(real, (real, "--remote", f"unix://{path}", *argv))
    except BaseException:
        listener.close()
        shutil.rmtree(directory)
        raise


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1], tuple(sys.argv[2:])))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        sys.exit(f"codex: {error}")
