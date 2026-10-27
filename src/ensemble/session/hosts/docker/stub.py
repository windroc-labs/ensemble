"""The container's hook command (stdlib only, run by path as `<hook path> hook <dir> <label>`): sends
its label and JSON payload to the guard's ENSEMBLE_GUARD_HOOK_SOCKET, resent with one delivery id
until acknowledged or WAIT seconds pass. It always exits 0, with one stderr line otherwise.
"""

import json
import os
import socket
import sys
import time

WAIT = 10


def main(argv=None, stdin=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    payload = (sys.stdin.buffer if stdin is None else stdin).read()
    if guard := os.environ.get("ENSEMBLE_GUARD_HOOK_SOCKET"):
        return _to_guard(args, payload, guard)
    print("ensemble hook: guard hook socket unavailable", file=sys.stderr)
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
