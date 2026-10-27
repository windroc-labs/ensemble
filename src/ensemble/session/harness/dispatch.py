"""A session harness's handlers: found over the session roots, every handler's roots, then
sys.path; loaded by on_event; each called in log order on its own daemon thread.
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from types import ModuleType
from typing import Any

from ensemble.session.engine.base import Session
from ensemble.session.engine.types import (
    ConfigError,
    HookCallback,
    HookError,
    SessionEvent,
    Stderr,
)
from ensemble.session.harness.channel import Channel
from ensemble.session.harness.types import EventCall, Logged
from ensemble.session.modes.tui_hook import find_module, load_callback

KIND = "event handler"
ERROR_MAX = 2000
BACKOFF, BACKOFF_MAX = 1, 30
LOG = logging.getLogger("session")


def load_handlers(
    handlers: Sequence[HookCallback], roots: Sequence[str], *, load: bool
) -> list[ModuleType | None]:
    """Each module (load False: None, nothing imported); ConfigError for one not found."""
    search = list(dict.fromkeys([*roots, *(root for h in handlers for root in h.roots), *sys.path]))
    found: list[ModuleType | None] = []
    for handler in handlers:
        try:
            specs = {handler.module: find_module(handler.module, search, KIND, HookError)}
            if not load:
                found.append(None)
                continue
            found.append(load_callback(handler.module, KIND, specs, entry="on_event"))
        except HookError as error:
            raise ConfigError(str(error)) from None
        except Exception as error:  # the module failed to load
            raise ConfigError(f"{KIND} {handler.module}: {error!r:.{ERROR_MAX}}") from error
    return found


class Dispatch:
    """A failure is retried with the same EventCall after BACKOFF s, doubling to BACKOFF_MAX;
    that handler's later events wait.
    """

    def __init__(
        self,
        handlers: Sequence[Any],
        states: Sequence[Mapping[str, object]],
        session: Session,
        channel: Channel,
        *,
        sid: str,
        generation: str,
        sleep: Callable[[float], object] | None = None,
    ) -> None:
        self._stopped = threading.Event()
        self._channel, self._session = channel, session
        self._sleep = sleep or self._stopped.wait
        self._sid, self._generation = sid, generation
        self._pending = 0
        self._lock = threading.Lock()
        self._settled = threading.Condition(self._lock)
        self._queues: list[queue.SimpleQueue[Logged | None]] = []
        self.threads: list[threading.Thread] = []
        for index, (handler, state) in enumerate(zip(handlers, states, strict=True)):
            waiting: queue.SimpleQueue[Logged | None] = queue.SimpleQueue()
            thread = threading.Thread(
                target=self._serve,
                args=(index, handler, state, waiting),
                name=f"session-handler-{index}",
                daemon=True,
            )
            self._queues.append(waiting)
            self.threads.append(thread)
            thread.start()

    def put(self, logged: Logged) -> None:
        """Stderr and ClientBound stay in the log."""
        if isinstance(logged.event, Stderr) or not isinstance(logged.event, SessionEvent):
            return
        with self._lock:
            self._pending += len(self._queues)
        for waiting in self._queues:
            waiting.put(logged)

    def pending(self) -> int:
        with self._lock:
            return self._pending

    def drain(self, timeout: float) -> int:
        """The number still pending after up to `timeout` s."""
        deadline = time.monotonic() + timeout
        with self._settled:
            while self._pending and (left := deadline - time.monotonic()) > 0:
                self._settled.wait(left)
            return self._pending

    def stop(self) -> None:
        self._stopped.set()
        for waiting in self._queues:
            waiting.put(None)

    def _serve(
        self,
        index: int,
        handler: Any,
        state: Mapping[str, object],
        waiting: queue.SimpleQueue[Logged | None],
    ) -> None:
        while (logged := waiting.get()) is not None:
            assert isinstance(logged.event, SessionEvent)
            call = EventCall(
                session=self._session,
                event=logged.event,
                sid=self._sid,
                generation=self._generation,
                event_id=logged.id,
                turn=logged.turn,
                state=state,
            )
            attempt, delay = 1, BACKOFF
            while True:
                try:
                    returned = handler.on_event(call)
                except BaseException as error:  # retried, SystemExit too
                    if attempt & (attempt - 1) == 0:  # attempts 1, 2, 4, …: a bounded record
                        LOG.warning("%s %d: %s failed", KIND, index, logged.id, exc_info=True)
                        self._note(index, logged, attempt, repr(error)[:ERROR_MAX])
                    self._sleep(delay)
                    if self._stopped.is_set():
                        return
                    attempt, delay = attempt + 1, min(delay * 2, BACKOFF_MAX)
                    continue
                if returned is None:
                    self._note(index, logged, attempt)
                else:
                    self._note(
                        index, logged, attempt, f"on_event returned {type(returned).__name__}"
                    )
                break
            with self._settled:
                self._pending -= 1
                self._settled.notify_all()

    def _note(self, index: int, logged: Logged, attempt: int, error: str | None = None) -> None:
        record = {
            "handler": index,
            "id": logged.id,
            "attempt": attempt,
            "state": "ok" if error is None else "failed",
            **({"error": error} if error is not None else {}),
        }
        try:
            self._channel.note(record)
        except OSError as failure:
            LOG.warning("session harness: deliveries.jsonl: %s", failure)
