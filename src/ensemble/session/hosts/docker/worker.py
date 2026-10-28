"""A harness-owned Docker worker: the guard, its container's main process, reached through one
`docker attach` and kept alive by a heartbeat. Every channel frame is untrusted: the client shares
the guard's uid. A frame can end its own worker; only confirmed container removal makes it ABSENT.
"""

import contextlib
import json
import os
import re
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping

from ensemble.session.engine.types import (
    Liveness,
    SessionError,
    WorkerHook,
)
from ensemble.session.harness import guard
from ensemble.session.hosts.base import GUARD_WAIT, sent
from ensemble.session.hosts.docker.hooks import HookWorker
from ensemble.session.hosts.docker.maintenance import RETRY, Maintenance, run
from ensemble.session.hosts.managed import QUEUE_MAX, ManagedProcess

GUARD = "/opt/ensemble/src/ensemble/session/harness/guard.py"
BEAT = 1.0  # s between heartbeats; the guard ends its worker after guard.HEARTBEAT s without one
LABEL = re.compile(r"[A-Za-z0-9_.-]{1,64}")
SEEN = 4096
HOOK_PAUSE = 1.0
HOOK_TRIES_FOR = 60.0
LOG_MAX = 1 << 20


def reject(state: str, generation: str, reason: str) -> None:
    """One rejected.jsonl line, never the payload; none once the log reaches LOG_MAX."""
    path = os.path.join(state, "rejected.jsonl")
    with contextlib.suppress(OSError):
        if os.path.exists(path) and os.path.getsize(path) >= LOG_MAX:
            return
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"generation": generation, "reason": reason}) + "\n")


def _pairs(pairs: list[tuple[str, object]]) -> dict:
    found = dict(pairs)
    if len(found) != len(pairs):
        raise ValueError("duplicate key")
    return found


def _constant(name: str) -> float:
    raise ValueError(f"non-finite number {name}")


def strict_hook(data: bytes) -> WorkerHook | str:
    """The frame's hook, else its rejection reason: size, shape or label."""
    if len(data) > guard.HOOK_MAX:
        return "size"
    try:
        json.loads(data, object_pairs_hook=_pairs, parse_constant=_constant)
        value = guard.hook_message(data)
    except (ValueError, UnicodeError, RecursionError):
        return "shape"
    if not LABEL.fullmatch(value["label"]):
        return "label"
    payload = value["payload"]
    payload.pop("transcript_path", None)
    return WorkerHook(value["label"], payload, value["delivery_id"])


class DockerWorker(ManagedProcess):
    """remove: its container's removal, raising SessionError; owner: the flocked owner-lock fd;
    hooks: the journal's resident hook process (None: hooks are read() as WorkerHook).
    """

    remote = True

    def __init__(
        self,
        name: str,
        generation: str,
        proc,
        *,
        state: str,
        events: str | None,
        hooks: HookWorker | None,
        remove: Callable[[], None],
        owner: int,
        maintenance: Callable[..., Maintenance],
        report: Callable[[str, str], None],
        terminal: bool = True,
        start_reader: bool = True,
    ) -> None:
        self.state_dir, self.events, self.hooks = state, events, hooks
        self.remove, self.owner = remove, owner
        self.seen: OrderedDict[str, None] = OrderedDict()
        self.hook_queue: deque[tuple[WorkerHook, int, str | None]] = deque()  # hook, size, sent
        self.hook_bytes = 0
        self.delivering = self.deliver_started = False
        self.hook_pause, self.hook_window = HOOK_PAUSE, HOOK_TRIES_FOR
        self.removed = self.ending = False
        self.attempts = 0
        self.removal_error: SessionError | None = None
        self.stopped = threading.Event()
        self.retry = threading.Event()
        self.ender: threading.Thread | None = None
        self.beater: threading.Thread | None = None
        self.report = report
        self.maintenance = maintenance(close=self._expire, report=self._report)
        super().__init__(name, generation, proc, terminal=terminal, start_reader=start_reader)

    def launch(self, handoff: Mapping) -> None:
        self.send_frame(b"l", json.dumps(handoff, separators=(",", ":")).encode())
        with self.condition:
            self.beater = threading.Thread(target=self._beat, name=f"{self.name}-beat", daemon=True)
            self.beater.start()

    def _beat(self) -> None:
        while True:
            with self.condition:
                if self.condition.wait_for(self._released, BEAT):
                    return
            try:
                self.send_frame(b"p", b"{}")
            except SessionError:
                return

    def end_releases(self) -> None:
        super().end_releases()
        with self.condition:
            beater = self.beater
        self._join(beater)

    def begin(self) -> None:
        """Called after the guard started."""
        threading.Thread(
            target=run, args=(self.maintenance, self.stopped), name=f"{self.name}-tick", daemon=True
        ).start()
        if self.hooks is not None:
            with self.condition:
                self.deliver_started = True
            threading.Thread(target=self._deliver, name=f"{self.name}-hooks", daemon=True).start()

    def accept_hook(self, data: bytes) -> WorkerHook | None:
        found = strict_hook(data)
        if isinstance(found, str):
            reject(self.state_dir, self.generation, found)
            return None
        if self.hooks is None:
            return found
        if found.delivery_id in self.seen or any(
            queued.delivery_id == found.delivery_id for queued, *_ in self.hook_queue
        ):
            return None
        if self.stopped.is_set():
            reject(self.state_dir, self.generation, "hook")
            self._remember(found.delivery_id)
            return None
        if self.hook_bytes + len(data) > QUEUE_MAX:
            self.fail("guard hook buffer exceeded 8 MiB; worker closed")
            return None
        self.hook_queue.append((found, len(data), sent(self.hooks.folder)))
        self.hook_bytes += len(data)
        self.condition.notify_all()
        return None

    def _deliver(self) -> None:
        assert self.hooks is not None
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.hook_queue or self.stopped.is_set())
                if self.stopped.is_set():
                    self._drop_queued()
                    return
                hook, size, marker = self.hook_queue[0]
                self.delivering = True
            deadline = time.monotonic() + self.hook_window
            while (
                self.hooks.call(hook.label, dict(hook.payload), hook.delivery_id, marker)
                is not None
            ):
                if time.monotonic() >= deadline or self.stopped.wait(self.hook_pause):
                    reject(self.state_dir, self.generation, "hook")
                    break
            with self.condition:
                self.hook_queue.popleft()
                self.hook_bytes -= size
                self._remember(hook.delivery_id)
                self.delivering = False
                self.condition.notify_all()

    def _drop_queued(self) -> None:
        """Under its condition."""
        for hook, *_ in self.hook_queue:
            reject(self.state_dir, self.generation, "hook")
            self._remember(hook.delivery_id)
        self.hook_queue.clear()
        self.hook_bytes = 0
        self.condition.notify_all()

    def _remember(self, delivery_id: str) -> None:
        """Under its condition."""
        self.seen[delivery_id] = None
        while len(self.seen) > SEEN:
            self.seen.popitem(last=False)

    def _report(self, word: str, text: str) -> None:
        if not self.stopped.is_set():
            self.report(word, text)

    def _hooks_pending(self) -> bool:
        return bool(self.hook_queue) or self.delivering

    def on_exit(self) -> None:
        self._start_end()

    def _expire(self) -> None:
        self._start_end()

    def _start_end(self) -> threading.Thread:
        with self.condition:
            if self.ender is None:
                self.ending = True
                self.ender = threading.Thread(
                    target=self._end, name=f"{self.name}-end", daemon=True
                )
                self.ender.start()
            return self.ender

    def _end(self) -> None:
        """Hooks no _deliver thread took (the channel failed before begin()) are rejected."""
        with self.condition:
            self.condition.wait_for(
                lambda: (
                    not self._hooks_pending()
                    or (self.failure is not None and not self.deliver_started)
                ),
                HOOK_TRIES_FOR,
            )
        if self.failure is None and self.code is None:
            with contextlib.suppress(SessionError):
                self.send_frame(b"c", b"{}")
            with self.condition:
                self.condition.wait_for(
                    lambda: self.code is not None or self.failure is not None, GUARD_WAIT
                )
        with contextlib.suppress(SessionError):
            self.end_releases()
        with contextlib.suppress(OSError, ValueError):
            self.proc.stdin.close()
        while True:
            try:
                self.remove()
            except SessionError as error:
                with self.condition:
                    self.removal_error, self.attempts = error, self.attempts + 1
                    self.condition.notify_all()
                self.retry.wait(RETRY)
                self.retry.clear()
                continue
            break
        with self.condition:
            self.stopped.set()
            if not self.deliver_started:
                self._drop_queued()
            self.condition.notify_all()
            self.condition.wait_for(lambda: not self.delivering, self.hook_window)
        if self.hooks is not None:
            self.hooks.stop()
        with contextlib.suppress(OSError):
            os.close(self.owner)
        with self.condition:
            self.removed = True
            self.attempts += 1
            self.condition.notify_all()

    def close(self) -> None:
        """Returns once its container's removal is confirmed; SessionError when it failed."""
        with self.condition:
            if self.removed:
                return
            attempts = self.attempts
        self._start_end()
        if attempts:
            self.retry.set()
        with self.condition:
            self.condition.wait_for(lambda: self.removed or self.attempts > attempts)
            if not self.removed:
                raise SessionError(f"docker session {self.name}: {self.removal_error}")

    def state(self) -> Liveness:
        if self.removed:
            return Liveness.ABSENT
        if self.failure is not None or (self.ending and self.code is None):
            return Liveness.UNKNOWN
        return Liveness.LIVE

    def returncode(self) -> int | None:
        """The guard's code once its container's removal is confirmed (its hooks journaled
        first); None before, or without one.
        """
        with self.condition:
            return self.code if self.removed else None

    def read(self, timeout: float):
        if self.removed:
            return []
        return super().read(timeout)

    def tick(self, now: float) -> float | None:
        return None if self.stopped.is_set() else self.maintenance.due
