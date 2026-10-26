"""launch may keep state; Launch.env (not resume) carries Access.env/Spec.env. events normalizes
output; JsonHeadless calls parse per JSON line.
"""

import json
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator

from ensemble.session.engine.types import (
    LOCAL_CONTEXT,
    ClientBound,
    ClientContext,
    ClientHome,
    Launch,
    SessionEvent,
    Spec,
    Stderr,
    TurnFailed,
)


class Headless(ABC):
    _context: ClientContext = LOCAL_CONTEXT

    def _home(self) -> ClientHome | None:
        return None

    def commit(self) -> None:
        return None

    @abstractmethod
    def launch(self, spec: Spec) -> Launch: ...

    @abstractmethod
    def events(self, lines: Iterable[str]) -> Iterator[SessionEvent | Stderr | ClientBound]:
        """FR-26 target: TurnStarted on accepted input, TurnEnd/TurnFailed on terminal records."""


class JsonHeadless(Headless):
    _invalid_output = "invalid JSON event"

    @abstractmethod
    def parse(self, record: dict) -> Iterable[SessionEvent | Stderr | ClientBound]: ...

    def events(self, lines: Iterable[str]) -> Iterator[SessionEvent | Stderr | ClientBound]:
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except (ValueError, RecursionError):
                record = None
            if isinstance(record, dict):
                yield from self.parse(record)
            else:
                yield Stderr(self._invalid_output)
                yield TurnFailed("invalid_output", self._invalid_output)
