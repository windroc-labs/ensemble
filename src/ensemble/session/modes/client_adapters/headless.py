"""launch may keep state; Launch.env (not resume) carries Access.env/Spec.env. events normalizes
output; JsonHeadless calls parse per JSON line.
"""

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
)
from ensemble.utils import json_records


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
    @abstractmethod
    def parse(self, record: dict) -> Iterable[SessionEvent | Stderr | ClientBound]: ...

    def events(self, lines: Iterable[str]) -> Iterator[SessionEvent | Stderr | ClientBound]:
        for record in json_records(lines):
            yield from self.parse(record)
