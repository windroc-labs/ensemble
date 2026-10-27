"""A Docker session's host run supervisor (argv: name, state dir, TUI dir, events or "", hook
command, generation, the supervisor.lock fd its starter holds, then connector, dead-wait and
dead-check argv as JSON). Each relay request that passes its checks and is newer than <gen>.seq goes,
as event <gen>.<seq>, to one resident `<hook> hook --serve <TUI dir>`; only success records seq,
failures request retry, and a retry reruns only the Host callback. Every line gets one reply. SIGTERM
closes the connector's stdin; lines already sent still run. A confirmed client exit writes the dead
line. The session's credentials are maintained when the broker says.
"""

import base64
import contextlib
import fcntl
import json
import logging
import os
import select
import signal
import subprocess
import sys
import threading
import time

from ensemble.session.engine.status_log import SessionStatusLog
from ensemble.session.hosts.docker.broker import Broker, hook_root
from ensemble.session.hosts.docker.maintenance import Maintenance, run
from ensemble.session.hosts.docker.relay import LINE_MAX, relay_request
from ensemble.utils import save

HOOK_WAIT = 60
CHECK_WAIT = 10
START_WAIT = 60
POLL = 0.5
LOG_MAX = 1 << 20
LOG = logging.getLogger("session")


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


def _body(payload: str) -> dict | None:
    try:
        found = json.loads(base64.b64decode(payload, validate=True))
    except (ValueError, RecursionError):
        return None
    return found if isinstance(found, dict) else None


class Supervisor:
    def __init__(
        self,
        name: str,
        state: str,
        tui_dir: str,
        events: str,
        hook: str,
        *,
        generation: str,
        run=None,
    ) -> None:
        self.name, self.state, self.tui_dir, self.events = name, state, tui_dir, events
        self.generation, self.worker = generation, HookWorker([hook, "hook", "--serve", tui_dir])
        self.hook = hook
        self.run, self.popen = run or subprocess.run, subprocess.Popen
        self.stopping, self.line_max = False, LINE_MAX
        self.seq_file = os.path.join(state, f"{generation}.seq")
        self.broker, self.clock = Broker(warning=self._audit_warning), time.time

    def reject(self, gen: str | None, seq: int | None, reason: str) -> None:
        path = os.path.join(self.state, "rejected.jsonl")
        with contextlib.suppress(OSError):
            if os.path.exists(path) and os.path.getsize(path) >= LOG_MAX:
                return
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"gen": gen, "seq": seq, "reason": reason}) + "\n")

    def handle(self, line: bytes) -> dict:
        request = relay_request(line)
        body = None if request is None else _body(request["payload"])
        if request is None or body is None:
            self.reject(None, None, "malformed")
            return {"ok": False}
        gen, seq = request["gen"], request["seq"]
        why = None
        if gen != self.generation:
            why = "generation"
        elif request["argv"][1] != self.tui_dir:
            why = "directory"
        elif seq > self._seen():
            body.pop("transcript_path", None)
            why = self.worker.call(request["argv"][2], body, f"{gen}.{seq}")
            if why:
                self.reject(gen, seq, why)
                return {"gen": gen, "seq": seq, "ok": False, "retry": True}
            with contextlib.suppress(OSError):
                save(self.seq_file, str(seq), durable=True)
        if why:
            self.reject(gen, seq, why)
        return {"gen": gen, "seq": seq, "ok": why is None}

    def _seen(self) -> int:
        try:
            with open(self.seq_file, encoding="ascii") as f:
                return int(f.read())
        except (OSError, ValueError):
            return 0

    def serve(self, requests, replies) -> None:
        """A reply per line until EOF; a line over LINE_MAX is discarded."""
        while line := requests.readline(self.line_max + 1):
            if line.endswith(b"\n"):
                reply = self.handle(line)
            else:
                self.reject(None, None, "malformed")
                while line and not line.endswith(b"\n"):
                    line = requests.readline(self.line_max + 1)
                reply = {"ok": False}
            if not self.stopping:
                with contextlib.suppress(OSError, ValueError):
                    replies.write(json.dumps(reply).encode() + b"\n")
                    replies.flush()

    def watch(self, wait: list[str], check: list[str], stopped, start: float = START_WAIT) -> None:
        """A failed wait is retried for `start` s, until one has succeeded."""
        deadline = time.monotonic() + start
        while not stopped.is_set():
            try:
                waited = self.run(
                    wait,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError:
                return
            if stopped.is_set():
                return
            if waited.returncode:
                if time.monotonic() >= deadline:
                    return
            elif self._dead(check):
                if self.events:
                    with contextlib.suppress(OSError, ValueError):
                        SessionStatusLog(self.events).write(self.name, "dead")
                return
            else:
                deadline = 0.0
            stopped.wait(POLL)

    def maintain(self, stopped) -> None:
        container = f"ensemble-{self.name}-{self.generation}"
        tick = Maintenance(
            self.name,
            self.state,
            self.generation,
            self.broker,
            report=self._report,
            root=hook_root(self.hook),
            close=lambda: self.broker.close(container),
            clock=self.clock,
        )
        run(tick, stopped, clock=self.clock)

    def _audit_warning(self, reason: str) -> None:
        if self.events:
            SessionStatusLog(self.events).write(self.name, "credential_audit_warning", reason)
        else:
            LOG.warning("docker session %s: %s", self.name, reason)

    def _report(self, word: str, text: str) -> None:
        """OSError, ValueError: not written."""
        if self.events:
            SessionStatusLog(self.events).write(self.name, word, text)
        else:
            LOG.warning("docker session %s: %s %s", self.name, word, text)

    def _dead(self, check: list[str]) -> bool:
        try:
            with self.popen(
                check, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            ) as proc:
                ready = select.select([proc.stdout], [], [], CHECK_WAIT)[0]
                out = os.read(proc.stdout.fileno(), 64) if ready else b""
                proc.kill()
        except OSError:
            return False
        return out.strip() == b"1"


def main(argv=None) -> int:
    try:
        name, state, tui_dir, events, hook, generation, fd, *commands = (
            sys.argv[1:] if argv is None else argv
        )
        lock = int(fd)
        relay, wait, check = (json.loads(command) for command in commands)
    except ValueError:
        return 2
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 1
    supervisor = Supervisor(name, state, tui_dir, events, hook, generation=generation)
    proc = subprocess.Popen(
        relay, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    stopped = threading.Event()

    def stop(_signum, _frame):
        stopped.set()
        supervisor.stopping = True
        if proc.stdin is not None:
            proc.stdin.close()

    signal.signal(signal.SIGTERM, stop)
    threading.Thread(target=supervisor.watch, args=(wait, check, stopped), daemon=True).start()
    threading.Thread(target=supervisor.maintain, args=(stopped,), daemon=True).start()
    supervisor.serve(proc.stdout, proc.stdin)
    stopped.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
