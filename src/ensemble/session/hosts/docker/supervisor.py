"""A Docker session's host run supervisor (argv: name, state dir, TUI dir, events or "", hook
command, generation, the supervisor.lock fd its starter holds, then connector, dead-wait and
dead-check argv as JSON). Each relay request that passes its checks and is newer than <gen>.seq runs
`<hook> hook <TUI dir> <label>`; only success records seq, failures request retry. Every line gets one reply. SIGTERM closes the connector's
stdin; lines already sent still run. A confirmed client exit writes the dead line. The session's
GitHub tokens (<gen>.github.json) are refreshed REFRESH_AHEAD s before the earliest expiry.
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
from collections.abc import Callable
from datetime import datetime

from ensemble.session.engine.status_log import SessionStatusLog
from ensemble.session.engine.types import SessionError
from ensemble.session.hosts.docker.broker import BROKER, Broker
from ensemble.session.hosts.docker.relay import LINE_MAX, relay_request
from ensemble.utils import read_regular, save

HOOK_WAIT = 60
CHECK_WAIT = 10
START_WAIT = 60
POLL = 0.5
LOG_MAX = 1 << 20
REFRESH_AHEAD = 600
REFRESH_RETRY = 60
LOG = logging.getLogger("session")
AUTH_EXPIRED = (
    f"Client authentication expired; run {BROKER} login {{client}} on the host, then resume"
)


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
        self.hook, self.generation = hook, generation
        self.run, self.popen = run or subprocess.run, subprocess.Popen
        self.stopping, self.line_max = False, LINE_MAX
        self.seq_file = os.path.join(state, f"{generation}.seq")
        self.broker, self.clock = Broker(), time.time

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
            why = self._hook(request["argv"][2], body)
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

    def _hook(self, label: str, body: dict) -> str | None:
        try:
            res = self.run(
                [self.hook, "hook", self.tui_dir, label],
                input=json.dumps(body).encode(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=HOOK_WAIT,
            )
        except subprocess.TimeoutExpired:
            return "hook timeout"
        except OSError:
            return "hook error"
        return f"hook exit {res.returncode}" if res.returncode else None

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

    def refresh(self, stopped) -> None:
        """Renew the generation and report classified failures."""
        refresh_tokens(
            self.name,
            self.state,
            self.generation,
            self.broker,
            stopped,
            failed=self._refresh_failed,
            expired=self._auth_expired,
            clock=self.clock,
        )

    def _refresh_failed(self, reason: str) -> None:
        if self.events:
            with contextlib.suppress(OSError, ValueError):
                SessionStatusLog(self.events).write(
                    self.name,
                    "client_refresh_failed"
                    if refreshes_client(self.state)
                    else "github_refresh_failed",
                    reason,
                )

    def _auth_expired(self, reason: str) -> None:
        LOG.warning("docker session %s: client_auth_expired %s", self.name, reason)
        if self.events:
            with contextlib.suppress(OSError, ValueError):
                SessionStatusLog(self.events).write(self.name, "client_auth_expired", reason)

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


def refreshes_client(state: str) -> bool:
    try:
        record = json.loads(read_regular(os.path.join(state, "host.json")) or b"")
    except (OSError, ValueError, RecursionError):
        return False
    return isinstance(record, dict) and record.get("credential_refresh") is True


def _recorded_client(state: str) -> str | None:
    try:
        client = json.loads(read_regular(os.path.join(state, "host.json")) or b"")["client"]
    except (ValueError, TypeError, KeyError):
        return None
    return client if isinstance(client, str) and client else None


def refresh_tokens(
    name: str,
    state: str,
    generation: str,
    broker: Broker,
    stopped: threading.Event,
    *,
    failed: Callable[[str], None],
    expired: Callable[[str], None],
    clock: Callable[[], float] = time.time,
) -> None:
    """Until stopped: the broker's refresh at the earliest saved expiry less REFRESH_AHEAD, its
    list saved; a failure goes to `failed` and retries in REFRESH_RETRY s. No list: returns.
    """
    if refreshes_client(state):
        _renew_client(
            name, state, generation, broker, stopped, failed=failed, expired=expired, clock=clock
        )
        return
    path = os.path.join(state, f"{generation}.github.json")
    try:
        with open(path, encoding="utf-8") as f:
            delay = _delay(json.load(f), clock())
    except (OSError, ValueError, KeyError, TypeError):
        return
    while delay is not None and not stopped.wait(delay):
        try:
            found = broker.refresh(
                f"ensemble-{name}-{generation}", os.path.join(state, f"{generation}.json")
            )
        except SessionError as error:
            failed(str(error))
            delay = REFRESH_RETRY
            continue
        with contextlib.suppress(OSError):
            save(path, json.dumps(found.github), durable=True)
        delay = _delay(found.github, clock())


def _client_deadline(state: str, generation: str) -> float:
    try:
        value = json.loads(
            read_regular(os.path.join(state, f"{generation}.credential.json")) or b""
        )
        if not isinstance(value, str):
            return 0.0
        return datetime.fromisoformat(value).timestamp()
    except (ValueError, TypeError, OSError):
        return 0.0


def _renew_client(name, state, generation, broker, stopped, *, failed, expired, clock) -> None:
    path = os.path.join(state, f"{generation}.github.json")

    def delay():
        client = _client_deadline(state, generation) - REFRESH_AHEAD - clock()
        try:
            github = _delay(json.loads(read_regular(path) or b"[]"), clock())
        except (ValueError, KeyError, TypeError):
            github = None
        return max(0.0, min(client, github) if github is not None else client)

    wait = delay()
    while not stopped.wait(wait):
        retry = False
        try:
            found = broker.refresh(
                f"ensemble-{name}-{generation}", os.path.join(state, f"{generation}.json")
            )
            save(path, json.dumps(found.github), durable=True)
        except (SessionError, OSError) as error:
            retry = True
            failed(str(error))
        remaining = _client_deadline(state, generation) - clock()
        if remaining <= 0:
            expired(AUTH_EXPIRED.format(client=_recorded_client(state) or "for this client"))
            while True:
                try:
                    broker.close(f"ensemble-{name}-{generation}")
                except SessionError:
                    if stopped.wait(REFRESH_RETRY):
                        return
                else:
                    return
        wait = min(remaining, REFRESH_RETRY if retry else max(REFRESH_RETRY, delay()))


def _delay(expiries: list, now: float) -> float | None:
    """Seconds until the earliest expires_at less REFRESH_AHEAD, at least 0; None for no token."""
    due = min((datetime.fromisoformat(e["expires_at"]).timestamp() for e in expiries), default=None)
    return None if due is None else max(0.0, due - REFRESH_AHEAD - now)


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
    threading.Thread(target=supervisor.refresh, args=(stopped,), daemon=True).start()
    supervisor.serve(proc.stdout, proc.stdin)
    stopped.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
