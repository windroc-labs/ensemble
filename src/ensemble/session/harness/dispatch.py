"""A session harness's registered handlers: each module found over the session roots, its own and
every other handler's roots, then sys.path, and loaded by its on_event entry; each logged event
handed to every handler in log order on that handler's own daemon thread.
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
    """Each handler's module once found (load: imported, its on_event checked; else None, nothing
    imported); ConfigError for any that cannot be.
    """
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
        except Exception as error:  # the module's own code failed while it loaded
            raise ConfigError(f"{KIND} {handler.module}: {error!r:.{ERROR_MAX}}") from error
    return found


class Dispatch:
    """One daemon thread and queue per handler: a handler's events wait behind its failed one,
    retried with the same EventCall after BACKOFF s, doubling to BACKOFF_MAX; a hung handler holds
    only its own thread.
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
        sleep: Callable[[float], object] = time.sleep,
    ) -> None:
        self._channel, self._session, self._sleep = channel, session, sleep
        self._sid, self._generation = sid, generation
        self._pending = 0
        self._lock = threading.Lock()
        self._settled = threading.Condition(self._lock)
        self._queues: list[queue.SimpleQueue[Logged]] = []
        self.threads: list[threading.Thread] = []
        for index, (handler, state) in enumerate(zip(handlers, states, strict=True)):
            waiting: queue.SimpleQueue[Logged] = queue.SimpleQueue()
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
        """Hands a logged SessionEvent to every handler; Stderr and ClientBound stay in the log."""
        if isinstance(logged.event, Stderr) or not isinstance(logged.event, SessionEvent):
            return
        with self._lock:
            self._pending += len(self._queues)
        for waiting in self._queues:
            waiting.put(logged)

    def pending(self) -> int:
        """Deliveries not yet done."""
        with self._lock:
            return self._pending

    def drain(self, timeout: float) -> int:
        """Waits up to `timeout` s for every delivery; the number still pending."""
        deadline = time.monotonic() + timeout
        with self._settled:
            while self._pending and (left := deadline - time.monotonic()) > 0:
                self._settled.wait(left)
            return self._pending

    def _serve(
        self,
        index: int,
        handler: Any,
        state: Mapping[str, object],
        waiting: queue.SimpleQueue[Logged],
    ) -> None:
        while True:
            logged = waiting.get()
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
                except (SystemExit, KeyboardInterrupt):
                    raise
                except BaseException as error:  # any handler failure is retried
                    LOG.warning("%s %d: %s failed", KIND, index, logged.id, exc_info=True)
                    self._note(index, logged, attempt, repr(error)[:ERROR_MAX])
                    self._sleep(delay)
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
