"""A Docker session's broker credential maintenance as a tick its owner drives."""

import contextlib
import json
import os
import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from ensemble.session.engine.types import SessionError
from ensemble.session.hosts.docker.broker import Broker, BrokerError, deadline_ok
from ensemble.utils import read_regular, save

RETRY = 60


def deadline_file(state: str, generation: str) -> str:
    """The last deadline the broker confirmed for `generation`."""
    return os.path.join(state, f"{generation}.deadline.json")


class Maintenance:
    """due: what the last tick returned, the earlier of the next call and the deadline (None:
    done); before the first, the construction time.
    """

    def __init__(
        self,
        name: str,
        state: str,
        generation: str,
        broker: Broker,
        *,
        report: Callable[[str, str], None],
        close: Callable[[], object],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.state, self.generation, self.broker = state, generation, broker
        self.report, self.close, self.clock = report, close, clock
        self.container = f"ensemble-{name}-{generation}"
        self.manifest = os.path.join(state, f"{generation}.json")
        self.deadline = _cached(deadline_file(state, generation))
        self.next: float | None = clock()
        self.failing = self.ticked = self.closing = False
        self.due: float | None = self.next

    def tick(self, now: float) -> float | None:
        """Does what is due at `now`; the next due time, None once done."""
        self.due = self._tick(now)
        return self.due

    def _tick(self, now: float) -> float | None:
        if self.closing:
            return self._close(now)
        if self.deadline is not None and self.ticked:
            at = datetime.fromisoformat(self.deadline["at"]).timestamp()
            if self.next is None or self.next > at:
                if now < at:
                    return at
                _tell(self.report, "client_auth_expired", self.deadline["reason"])
                self.closing = True
                return self._close(now)
        if self.next is None or now < self.next:
            return self.next
        self.next = self._maintain(now)
        if self.deadline is None:
            return self.next
        at = datetime.fromisoformat(self.deadline["at"]).timestamp()
        return at if self.next is None else min(self.next, at)

    def _close(self, now: float) -> float | None:
        try:
            self.close()
        except SessionError:
            return now + RETRY
        return None

    def _maintain(self, now: float) -> float | None:
        """The next call's time."""
        self.ticked = True
        try:
            found = self.broker.maintain(self.container, self.manifest)
        except SessionError as error:
            if not self.failing:
                why = str(error) if isinstance(error, BrokerError) else "transport"
                _tell(self.report, "credential_maintenance_failed", why)
            self.failing = True
            return self.clock() + RETRY
        self.failing = False
        for result in found.results:
            if (failed := result["error"]) is not None:
                cause = f" ({failed['cause']})" if "cause" in failed else ""
                _tell(
                    self.report,
                    "credential_refresh_failed",
                    f"{result['credential']}: {failed['message']}{cause}",
                )
        if found.deadline is not None:
            self.deadline = found.deadline
            with contextlib.suppress(OSError):
                save(
                    deadline_file(self.state, self.generation),
                    json.dumps(self.deadline),
                    durable=True,
                )
        return _timestamp(found.next_check)


def run(
    maintenance: Maintenance, stopped: threading.Event, clock: Callable[[], float] = time.time
) -> None:
    """Ticks `maintenance` at each due time until it is done or `stopped` is set."""
    at = maintenance.due
    while at is not None:
        if stopped.wait(max(0.0, at - clock())):
            return
        at = maintenance.tick(clock())


def _tell(report: Callable[[str, str], None], word: str, text: str) -> None:
    with contextlib.suppress(OSError, ValueError):
        report(word, text)


def _timestamp(value: str | None) -> float | None:
    return None if value is None else datetime.fromisoformat(value).timestamp()


def _cached(path: str) -> Any:
    try:
        found = json.loads(read_regular(path) or b"null")
    except (OSError, ValueError, RecursionError):
        return None
    return found if deadline_ok(found) else None
