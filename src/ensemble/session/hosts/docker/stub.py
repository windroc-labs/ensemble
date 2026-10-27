"""The container's hook command (stdlib only, run by path as `<hook path> hook <dir> <label>`): sends
its argv and stdin to the relay daemon at SOCKET and waits WAIT seconds for it to queue them; under a
guard (ENSEMBLE_GUARD_HOOK_SOCKET), its label and JSON payload to the guard's socket, resent with the
same delivery id until acknowledged or WAIT seconds pass. It always exits 0, with one stderr line
otherwise, so a missing relay or guard never fails the client.
"""

import base64
import json
import os
import socket
import sys
import time

SOCKET = "/run/ensemble/relay.sock"
WAIT = 10


def main(argv=None, stdin=None, path: str = SOCKET) -> int:
    args = sys.argv[1:] if argv is None else argv
    payload = (sys.stdin.buffer if stdin is None else stdin).read()
    if guard := os.environ.get("ENSEMBLE_GUARD_HOOK_SOCKET"):
        return _to_guard(args, payload, guard)
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


def _to_guard(args: list[str], payload: bytes, path: str) -> int:
    if len(args) != 3 or args[0] != "hook":
        print("ensemble hook: usage: hook DIR LABEL", file=sys.stderr)
        return 0
    try:
        body = json.loads(payload)
    except (ValueError, RecursionError):
        body = None
    if not isinstance(body, dict):
        print("ensemble hook: payload is not a JSON object", file=sys.stderr)
        return 0
    delivery = os.urandom(16).hex()
    request = json.dumps({"label": args[2], "payload": body, "delivery_id": delivery}).encode()
    deadline = time.monotonic() + WAIT
    while (left := deadline - time.monotonic()) > 0:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(left)
                conn.connect(path)
                conn.sendall(request + b"\n")
                with conn.makefile("rb") as reader:
                    reply = reader.readline(4096)
            if json.loads(reply) == {"ok": True, "delivery_id": delivery}:
                return 0
        except (OSError, ValueError):
            pass
        time.sleep(0.2)
    print("ensemble hook: guard did not acknowledge", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
