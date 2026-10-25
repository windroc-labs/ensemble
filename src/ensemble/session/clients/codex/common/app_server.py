"""The Codex app-server JSON-RPC client, imported by Codex startup. Run by path using only the stdlib,
it is the account probe: prints the account/rateLimits/read result, or null on any failure
(--timeout S, 0 < S <= 10).
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import signal
import subprocess
import sys
import time
from abc import ABC, abstractmethod


class Termination(ABC):
    signum: int

    @abstractmethod
    def check(self) -> None: ...


class Error(Exception):
    pass


class Exited(Exception):
    pass


POLL = 0.1

BLOCK = 1 << 16

EXIT_WAIT = 5

PROBE_MAX = 1 << 20


class AppServer:
    """Own one app-server process and close its group after a single initialized request."""

    def __init__(
        self,
        proc: subprocess.Popen[bytes],
        wait: float,
        limit: int,
        termination: Termination | None,
        exit_wait: float,
    ):
        self._proc = proc
        assert proc.stdin is not None and proc.stdout is not None
        self._stdin, self._stdout = proc.stdin, proc.stdout
        self._termination, self._exit_wait = termination, exit_wait
        self._lines = Lines(self._stdout.fileno(), wait, limit, termination=termination)

    @classmethod
    def call(
        cls,
        exe: str,
        args: list[str],
        method: str,
        params: dict | None = None,
        *,
        env: dict[str, str],
        wait: float,
        limit: int,
        cwd: str | None = None,
        termination: Termination | None = None,
        exit_wait: float = EXIT_WAIT,
    ):
        """Own a single initialized request's process from spawning through cleanup."""
        client = None
        exited = False
        try:
            if termination is not None:
                termination.check()
            try:
                proc = subprocess.Popen(
                    [exe, "app-server", *args],
                    cwd=cwd,
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError as e:
                raise Error(f"{exe}: {e.strerror}") from e
            client = cls(proc, wait, limit, termination, exit_wait)
            if termination is not None:
                termination.check()
            return client._request(method, params)
        except Exited:
            exited = True
            raise
        finally:
            if client is not None:
                client._close()
            if termination is not None:
                termination.check()
            if exited and client is not None:
                raise Exited(client._proc.returncode)

    def _request(self, method: str, params: dict | None):
        self._send(
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "ensemble", "version": "0"},
                    "capabilities": {"experimentalApi": True},
                },
            }
        )
        self._answer(1, "initialize")
        self._send({"method": "initialized"})
        self._send(
            {"id": 2, "method": method, **({"params": params} if params is not None else {})}
        )
        return self._answer(2, method)

    def _send(self, message: dict) -> None:
        try:
            self._stdin.write(json.dumps(message).encode() + b"\n")
            self._stdin.flush()
        except BrokenPipeError:
            raise Exited from None

    def _answer(self, ident: int, method: str):
        while True:
            try:
                message = json.loads(next(self._lines))
            except (ValueError, RecursionError):
                continue
            if (
                not isinstance(message, dict)
                or "method" in message
                or type(message.get("id")) is not int
                or message["id"] != ident
            ):
                continue
            if "result" in message:
                return message["result"]
            error = message.get("error")
            detail = error.get("message") if isinstance(error, dict) else error
            raise Error(f"{method} failed: {detail}")

    def _close(self) -> None:
        deadline = time.monotonic() + self._exit_wait
        with contextlib.suppress(OSError):
            self._stdin.close()
        fd = self._stdout.fileno()
        try:
            for interval in _polls(deadline, self._termination):
                if select.select([fd], [], [], interval)[0]:
                    if not os.read(fd, BLOCK):
                        break
                elif self._proc.poll() is not None:
                    break
            for interval in _polls(deadline, self._termination):
                try:
                    self._proc.wait(timeout=interval)
                    break
                except subprocess.TimeoutExpired:
                    pass
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self._proc.pid, signal.SIGKILL)
            self._proc.wait()
            self._stdout.close()


class Lines:
    def __init__(self, fd: int, wait: float, limit: int, *, termination: Termination | None = None):
        self.fd, self.wait, self.limit, self.termination = fd, wait, limit, termination
        self.deadline, self.buf, self.seen = time.monotonic() + wait, bytearray(), 0

    def __next__(self) -> bytes:
        while (end := self.buf.find(b"\n", self.seen)) < 0:
            if self.termination is not None:
                self.termination.check()
            self.seen = len(self.buf)
            left = self.deadline - time.monotonic()
            if left <= 0:
                raise Error(f"no answer in {self.wait} s")
            if not select.select([self.fd], [], [], min(left, POLL))[0]:
                continue
            chunk = os.read(self.fd, BLOCK)
            if not chunk:
                raise Exited
            if len(self.buf) + len(chunk) > self.limit:
                raise Error(f"an answer over {self.limit} bytes")
            self.buf += chunk
        line = bytes(self.buf[:end])
        del self.buf[: end + 1]
        self.seen = 0
        return line


def _polls(deadline: float, termination):
    while (left := deadline - time.monotonic()) > 0:
        if termination is not None and termination.signum:
            return
        yield min(left, POLL)


def probe(env: dict, timeout: float = 10) -> dict | None:
    if type(timeout) not in (int, float) or not 0 < timeout <= 10:
        return None
    try:
        result = AppServer.call(
            "codex",
            [],
            "account/rateLimits/read",
            env=env,
            wait=timeout,
            limit=PROBE_MAX,
            exit_wait=POLL,
        )
    except (Error, Exited, OSError, ValueError):
        return None
    return result if isinstance(result, dict) else None


def main():
    argv = sys.argv[1:]
    try:
        timeout = (
            10 if not argv else float(argv[1]) if len(argv) == 2 and argv[0] == "--timeout" else 0
        )
        output = json.dumps(probe(dict(os.environ), timeout), allow_nan=False)
    except (ValueError, RecursionError):
        output = "null"
    print(output)


if __name__ == "__main__":
    main()
