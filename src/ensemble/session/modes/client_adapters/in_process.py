"""check refuses with ConfigError. Today run uses the caller's thread, stopping at its next yield;
no global cwd/env/signals change; applies access.cwd, not env. Target: harness thread.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterable

from ensemble.session.engine.types import ClientBound, SessionEvent, Spec, Stderr


class InProcess(ABC):
    @abstractmethod
    def run(self, spec: Spec) -> Iterable[SessionEvent | Stderr | ClientBound]:
        """FR-26 target: in the harness, TurnStarted on accepted input."""

    def check(self, spec: Spec) -> None:
        return None
