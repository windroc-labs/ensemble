"""The container's hook command (stdlib only, run by path as `<hook path> hook <dir> <label>`): sends
its argv and stdin to the relay daemon at SOCKET and waits WAIT seconds for it to queue them. It
always exits 0, with one stderr line otherwise, so a missing relay never fails the client.
"""

import base64
import json
import os
import socket
import sys

SOCKET = "/run/ensemble/relay.sock"
WAIT = 10


def main(argv=None, stdin=None, path: str = SOCKET) -> int:
    args = sys.argv[1:] if argv is None else argv
    payload = (sys.stdin.buffer if stdin is None else stdin).read()
    request = {
        "id": int.from_bytes(os.urandom(6), "big"),
        "argv": args,
        "payload": base64.b64encode(payload).decode(),
    }
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(WAIT)
            conn.connect(path)
            conn.sendall(json.dumps(request).encode() + b"\n")
            with conn.makefile("rb") as reader:
                reply = reader.readline(4096)
    except OSError as error:
        print(f"ensemble hook relay: {error}", file=sys.stderr)
        return 0
    try:
        found = json.loads(reply) if reply.endswith(b"\n") else None
    except ValueError:
        found = None
    if not (
        isinstance(found, dict)
        and found.get("queued") is True
        and found == {"id": request["id"], "queued": True}
    ):
        print("ensemble hook relay: not queued", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
