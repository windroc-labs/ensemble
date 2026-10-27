"""One resident `<hook> hook --serve <TUI dir>` process journaling a Docker session's hooks on the
host (WDRC-182: no Python process per hook).
"""

import contextlib
import json
import os
import select
import subprocess
import time

HOOK_WAIT = 60


class HookWorker:
    """`argv`, one resident `hook --serve` process, started by the first call and by the first after
    it ends; each call is bounded by `wait` s, and a timeout kills it.
    """

    def __init__(self, argv: list[str]) -> None:
        self.argv, self.popen, self.wait = argv, subprocess.Popen, HOOK_WAIT
        self.proc: subprocess.Popen | None = None

    def call(self, label: str, body: dict, event: str) -> str | None:
        """None on success, else the reject reason."""
        deadline = time.monotonic() + self.wait
        proc = self.proc
        if proc is not None and proc.poll() is not None:
            self._end(proc)
            proc = None
        if proc is None:
            try:
                proc = self.proc = self.popen(
                    self.argv,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
            except OSError:
                return "hook error"
        line = json.dumps({"label": label, "event": event, "payload": body}).encode() + b"\n"
        try:
            reply = _exchange(proc, line, deadline)
        except OSError:  # it ended mid-write
            reply = b""
        if reply is None:
            self._end(proc)
            return "hook timeout"
        if not reply.endswith(b"\n"):
            return f"hook exit {self._end(proc, deadline - time.monotonic())}"
        return None if reply == b'{"ok": true}\n' else "hook exit 1"

    def stop(self) -> None:
        """Ends its process, if one runs."""
        if self.proc is not None:
            self._end(self.proc, self.wait)

    def _end(self, proc: subprocess.Popen, wait: float = 0.0) -> int:
        """Its exit code, killed unless it exits within `wait` s."""
        self.proc = None
        try:
            proc.wait(timeout=max(0.0, wait))
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        for stream in (proc.stdin, proc.stdout):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()
        return proc.returncode


def _exchange(proc: subprocess.Popen, line: bytes, deadline: float) -> bytes | None:
    """The reply line, or what came before EOF; None past the deadline. Each write after select
    is at most PIPE_BUF bytes, which never blocks.
    """
    assert proc.stdin is not None and proc.stdout is not None
    sink, source = proc.stdin.fileno(), proc.stdout.fileno()
    while line:
        if not select.select([], [sink], [], max(0.0, deadline - time.monotonic()))[1]:
            return None
        line = line[os.write(sink, line[: select.PIPE_BUF]) :]
    reply = b""
    while not reply.endswith(b"\n"):
        if not select.select([source], [], [], max(0.0, deadline - time.monotonic()))[0]:
            return None
        if not (chunk := os.read(source, 4096)):
            break
        reply += chunk
    return reply
