"""A harness-owned Docker worker: the guard in its container, reached through one `docker exec -i`
(docs/docker-host.md › Harness-owned workers). Every channel frame is untrusted: the client shares
the guard's uid. A frame can end its own worker; only confirmed container removal makes it ABSENT.
"""

import contextlib
import json
import os
import re
import signal
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping

from ensemble.session.engine.types import (
    Key,
    Liveness,
    Paste,
    SessionError,
    StatusState,
    WorkerHook,
)
from ensemble.session.harness import guard
from ensemble.session.hosts.base import InteractiveProcess
from ensemble.session.hosts.docker.activity import activity, write_activity
from ensemble.session.hosts.docker.hooks import HookWorker
from ensemble.session.hosts.docker.maintenance import RETRY, Maintenance, run
from ensemble.session.hosts.managed import QUEUE_MAX, WAIT, ManagedProcess
from ensemble.utils import Record

GUARD = "/opt/ensemble/src/ensemble/session/harness/guard.py"
RUN = "/run/ensemble/guard"
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


class _Record:
    """The Host record's fields while it is this generation's."""

    def __init__(self, name: str, generation: str, load, store) -> None:
        self.name, self.generation, self.load, self.store = name, generation, load, store

    def own(self) -> dict:
        found = self.load()
        return found if found.get("generation") == self.generation else {}

    def publish(self, record: Record) -> None:
        found = self.own()
        if not found:
            raise SessionError(f"docker session {self.name} is gone")
        self.store({**found, "record": record.to_line()})

    def record(self) -> Record | None:
        line = self.own().get("record")
        with contextlib.suppress(ValueError, TypeError):
            return None if line is None else Record.parse(line)
        return None

    def display(self):
        value = self.own().get("display")
        return value if isinstance(value, str) else None

    def session_identity(self) -> tuple[str | None, str | None]:
        found = self.own()
        return found.get("sid"), found.get("generation")


class DockerWorker(ManagedProcess):
    """remove: its container's removal, raising SessionError; owner: the flocked owner-lock fd;
    hooks: the journal's resident hook process (None: hooks are read() as WorkerHook); load and
    store: its Host record.
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
        load: Callable[[], dict],
        store: Callable[[Mapping], None],
        terminal: bool = True,
        start_reader: bool = True,
    ) -> None:
        self.state_dir, self.events, self.hooks = state, events, hooks
        self.remove, self.owner = remove, owner
        self.host_record = _Record(name, generation, load, store)
        self.seen: OrderedDict[str, None] = OrderedDict()
        self.hook_queue: deque[tuple[WorkerHook, int]] = deque()
        self.hook_bytes = 0
        self.delivering = False
        self.hook_pause, self.hook_window = HOOK_PAUSE, HOOK_TRIES_FOR
        self.removed = self.ending = False
        self.attempts = 0
        self.removal_error: SessionError | None = None
        self.stopped = threading.Event()
        self.retry = threading.Event()
        self.ender: threading.Thread | None = None
        self.report = report
        self.maintenance = maintenance(close=self._expire, report=self._report)
        super().__init__(name, generation, proc, terminal=terminal, start_reader=start_reader)

    @property
    def identity(self) -> str:
        return f"{self.name} {self.generation}"

    def begin(self) -> None:
        """Its maintenance thread and, journaling, its hook thread; after the guard started."""
        threading.Thread(
            target=run, args=(self.maintenance, self.stopped), name=f"{self.name}-tick", daemon=True
        ).start()
        if self.hooks is not None:
            threading.Thread(target=self._deliver, name=f"{self.name}-hooks", daemon=True).start()

    def accept_hook(self, data: bytes) -> WorkerHook | None:
        found = strict_hook(data)
        if isinstance(found, str):
            reject(self.state_dir, self.generation, found)
            return None
        if self.hooks is None:
            return found
        if found.delivery_id in self.seen or any(
            queued.delivery_id == found.delivery_id for queued, _ in self.hook_queue
        ):
            return None
        if self.hook_bytes + len(data) > QUEUE_MAX:
            self.fail("guard hook buffer exceeded 8 MiB; worker closed")
            return None
        self.hook_queue.append((found, len(data)))
        self.hook_bytes += len(data)
        self.condition.notify_all()
        return None

    def _deliver(self) -> None:
        assert self.hooks is not None
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.hook_queue or self.stopped.is_set())
                if not self.hook_queue:
                    return
                hook, size = self.hook_queue[0]
                self.delivering = True
            deadline = time.monotonic() + self.hook_window
            while self.hooks.call(hook.label, dict(hook.payload), hook.delivery_id) is not None:
                if time.monotonic() >= deadline or self.stopped.wait(self.hook_pause):
                    reject(self.state_dir, self.generation, "hook")
                    break
            with self.condition:
                self.hook_queue.popleft()
                self.hook_bytes -= size
                self.seen[hook.delivery_id] = None
                while len(self.seen) > SEEN:
                    self.seen.popitem(last=False)
                self.delivering = False
                self.condition.notify_all()

    def _report(self, word: str, text: str) -> None:
        """Maintenance's report, dropped once the worker is gone."""
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
        """Its hooks journaled, the guard told to close, then the container removed, retried."""
        with self.condition:
            self.condition.wait_for(lambda: not self._hooks_pending(), HOOK_TRIES_FOR)
        if self.failure is None and self.code is None:
            with contextlib.suppress(SessionError):
                self.send_frame(b"c", b"{}")
            with self.condition:
                self.condition.wait_for(
                    lambda: self.code is not None or self.failure is not None, WAIT
                )
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
        self.stopped.set()
        with self.condition:
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

    def alive(self) -> bool:
        return self.state() == Liveness.LIVE

    def wait_exit(self, timeout: float) -> bool:
        with self.condition:
            self.condition.wait_for(lambda: self.removed, timeout)
            return self.removed

    def kill(self) -> None:
        self.close()

    def activity(self) -> StatusState | None:
        return activity(self.state_dir, self.generation)

    def mark(self, word: StatusState, events: str | None) -> None:
        try:
            write_activity(self.state_dir, self.generation, word, events, self.name)
        except (OSError, ValueError) as error:
            raise SessionError(f"docker session {self.name}: {error}") from None

    def publish(self, record: Record) -> None:
        self.host_record.publish(record)

    def record(self) -> Record | None:
        return self.host_record.record()

    def display(self):
        return self.host_record.display()

    def session_identity(self) -> tuple[str | None, str | None]:
        return self.host_record.session_identity()


class GuardedView(InteractiveProcess):
    """A guarded worker seen from a process that does not hold it: its Host record, activity and
    owner lock; its harness alone sends, presses and reads its screen.
    """

    def __init__(
        self,
        name: str,
        generation: str,
        *,
        state: str,
        load: Callable[[], dict],
        liveness: Callable[[], Liveness],
        remove: Callable[[], None],
    ) -> None:
        self.name, self.generation, self.state_dir = name, generation, state
        self.host_record = _Record(name, generation, load, lambda _: None)
        self._liveness, self._remove = liveness, remove

    @property
    def identity(self) -> str:
        return f"{self.name} {self.generation}"

    def state(self) -> Liveness:
        return self._liveness()

    def alive(self) -> bool:
        return self.state() == Liveness.LIVE

    def returncode(self) -> int | None:
        return -signal.SIGHUP if self.state() == Liveness.ABSENT else None

    def _owned(self) -> SessionError:
        return SessionError(f"docker session {self.name}: owned by its harness")

    def send(self, action: Paste | Key) -> None:
        raise self._owned()

    def press(self, keys) -> None:
        raise self._owned()

    def screen(self, lines: int | None = None) -> str:
        raise self._owned()

    def close(self) -> None:
        self._remove()

    def wait_exit(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while self.state() != Liveness.ABSENT:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.2)
        return True

    def kill(self) -> None:
        self.close()

    def activity(self) -> StatusState | None:
        return activity(self.state_dir, self.generation)

    def record(self) -> Record | None:
        return self.host_record.record()

    def display(self):
        return self.host_record.display()

    def session_identity(self) -> tuple[str | None, str | None]:
        return self.host_record.session_identity()
