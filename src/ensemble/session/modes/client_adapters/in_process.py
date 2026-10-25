"""check(spec) refuses with ConfigError; run(spec) runs the session on a thread of the caller's
process, stopped at its next yield, changing no global state (cwd, environ, signals): it applies
spec.access.cwd, not its env.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterable

from ensemble.session.engine.types import ClientBound, SessionEvent, Spec, Stderr


class InProcess(ABC):
    @abstractmethod
    def run(self, spec: Spec) -> Iterable[SessionEvent | Stderr | ClientBound]: ...

    def check(self, spec: Spec) -> None:
        return None
