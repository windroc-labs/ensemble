"""Independent native TUI/server stand-ins sharing one server environment and socket."""

import base64
import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import uuid


def exact(stream, size):
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            raise EOFError
        data.extend(chunk)
    return bytes(data)


def receive(stream):
    first, second = exact(stream, 2)
    size = second & 127
    if size == 126:
        size = struct.unpack("!H", exact(stream, 2))[0]
    elif size == 127:
        size = struct.unpack("!Q", exact(stream, 8))[0]
    mask = exact(stream, 4) if second & 128 else b""
    data = exact(stream, size)
    if mask:
        data = bytes(value ^ mask[index % 4] for index, value in enumerate(data))
    if first & 15 == 8:
        raise EOFError
    return json.loads(data)


def send(connection, message, *, masked=False):
    payload = json.dumps(message).encode()
    size = len(payload)
    length = bytes([size]) if size < 126 else b"\x7e" + struct.pack("!H", size)
    mask = b"test" if masked else b""
    if mask:
        payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    connection.sendall(
        bytes([129, length[0] | (128 if masked else 0)]) + length[1:] + mask + payload
    )


def header(connection):
    data = bytearray()
    while not data.endswith(b"\r\n\r\n"):
        chunk = connection.recv(1)
        if not chunk:
            raise EOFError
        data.extend(chunk)
    return bytes(data)


def handle(connection):
    with connection:
        try:
            request = header(connection)
            assert b"x-codex-app-server-compression" not in request.lower()
            key = next(
                line.split(b":", 1)[1].strip()
                for line in request.split(b"\r\n")
                if line.lower().startswith(b"sec-websocket-key:")
            )
            accept = base64.b64encode(
                hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest()
            )
            connection.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: "
                + accept
                + b"\r\n\r\n"
            )
            source, sid, cwd, policy = "startup", "", "", {}
            with connection.makefile("rb") as stream:
                while True:
                    message = receive(stream)
                    method, params = message.get("method"), message.get("params", {})
                    result = {}
                    if method in {"thread/start", "thread/resume"}:
                        sid = params.get("threadId") or str(uuid.uuid4())
                        source = (
                            "resume"
                            if method == "thread/resume"
                            else params.get("sessionStartSource", "startup")
                        )
                        cwd = params.get("cwd", os.getcwd())
                        policy = params["config"]["shell_environment_policy"]
                        result = {"thread": {"id": sid, "cwd": cwd}}
                    elif method == "turn/start":
                        root = os.environ["PLUGIN_ROOT"]
                        with open(
                            os.path.join(root, "hooks", "manual.json"), encoding="utf-8"
                        ) as f:
                            command = json.load(f)["hooks"]["SessionStart"][0]["hooks"][0][
                                "command"
                            ]
                        hook = subprocess.run(
                            ("sh", "-c", command),
                            input=json.dumps({"session_id": sid, "cwd": cwd, "source": source}),
                            capture_output=True,
                            text=True,
                            check=True,
                            timeout=10,
                        )
                        result = {
                            "hook": json.loads(hook.stdout),
                            "shell_env": policy["set"],
                            "daemon": os.getpid(),
                        }
                    if "id" in message:
                        send(connection, {"id": message["id"], "result": result})
        except (EOFError, ConnectionError):
            return


def daemon(path):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(path)
        listener.listen()
        print("ready", flush=True)
        while True:
            connection, _address = listener.accept()
            threading.Thread(target=handle, args=(connection,), daemon=True).start()


def tui(argv):
    path = argv[argv.index("--remote") + 1].removeprefix("unix://")
    resumed = "resume" in argv
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect(path)
        connection.sendall(
            b"GET /rpc HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nx-codex-app-server-compression: zstd-stream-v1\r\n\r\n"
        )
        assert header(connection).startswith(b"HTTP/1.1 101 ")
        with connection.makefile("rb") as stream:
            counter = 0

            def call(method, params):
                nonlocal counter
                counter += 1
                send(connection, {"id": counter, "method": method, "params": params}, masked=True)
                return receive(stream)["result"]

            call("initialize", {"clientInfo": {"name": "fake-tui", "version": "0"}})
            params = {}
            if "--cd" in argv:
                params["cwd"] = argv[argv.index("--cd") + 1]
            elif "-C" in argv:
                params["cwd"] = argv[argv.index("-C") + 1]
            if resumed:
                params["threadId"] = argv[argv.index("resume") + 1]
            result = call("thread/resume" if resumed else "thread/start", params)
            print(json.dumps({"pid": os.getpid(), "sid": result["thread"]["id"]}), flush=True)
            for line in sys.stdin:
                action = line.strip()
                if action == "quit":
                    return
                if action == "clear":
                    result = call(
                        "thread/start",
                        {
                            **{k: v for k, v in params.items() if k == "cwd"},
                            "sessionStartSource": "clear",
                        },
                    )
                    print(
                        json.dumps({"pid": os.getpid(), "sid": result["thread"]["id"]}), flush=True
                    )
                else:
                    print(json.dumps(call("turn/start", {})), flush=True)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--fake-daemon"]:
        daemon(sys.argv[2])
    elif sys.argv[1:4] == ["app-server", "daemon", "start"]:
        print(json.dumps({"socketPath": os.environ["FAKE_DAEMON_SOCKET"]}))
    elif os.environ.get("FAKE_PASSTHROUGH"):
        print(json.dumps(sys.argv[1:]))
        sys.exit(23)
    else:
        tui(sys.argv[1:])
