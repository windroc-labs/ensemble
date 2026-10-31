from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence

from ensemble.delegate.composer import RunConfig
from ensemble.delegate.engine.types import Event, Outer, Result, Spec
from ensemble.delegate.event.outcome import Outcome
from ensemble.session import SessionStatus, Usage
from ensemble.utils import Display, Record, Status


class Delegate(ABC):
    """One role run and its view. It touches only the entries it creates in its workdir W."""

    @classmethod
    @abstractmethod
    def create(
        cls,
        role: str,
        spec: Spec,
        *,
        display: Display = None,
        callback: Sequence[str] | None = None,
        overlays: Sequence[Mapping] = (),
    ) -> Delegate: ...

    @classmethod
    @abstractmethod
    def attach(cls, record_or_workdir: Record | str, *, sid: str | None = None) -> Delegate:
        """ConfigError when `sid` is given and the run is another session's."""

    @classmethod
    @abstractmethod
    def usage(cls, role: str, *, overlays: Sequence[Mapping] = ()) -> Usage | None: ...

    @classmethod
    @abstractmethod
    def roles(cls) -> dict[str, dict[str, str]]: ...

    @classmethod
    @abstractmethod
    def resolve(
        cls, role: str, task: str | None = None, *, overlays: Sequence[Mapping] = ()
    ) -> RunConfig: ...

    @classmethod
    @abstractmethod
    def trusted_dirs(cls) -> frozenset[str]: ...

    @classmethod
    @abstractmethod
    def checkouts(cls, workdir: str) -> tuple[str, ...] | None:
        """Real paths; None without agent files."""

    @classmethod
    @abstractmethod
    def clean(cls, workdir: str, *, dry: bool = False, scratch: bool = False) -> bool:
        """Deletes W's agent files (scratch: checkouts and temp files), keeping run records;
        idempotent. ConfigError, nothing deleted, while the run may be live.
        """

    @classmethod
    @abstractmethod
    def check_tui(cls, *, display: Display = None, events: str | None = None) -> None: ...

    @abstractmethod
    def detach(self, *, outer: Outer | None = None) -> Record: ...

    @abstractmethod
    def run(self, on_event: Callable[[Event], None] | None = None) -> Result:
        """Handlers stay registered."""

    @abstractmethod
    def alive(self) -> bool:
        """Independent of worker liveness."""

    @abstractmethod
    def status(self) -> Status:
        """needs_input: waiting, not alive."""

    @abstractmethod
    def content(self) -> str: ...

    @abstractmethod
    def outcome(self) -> Outcome | None: ...

    @abstractmethod
    def show(self) -> str | None: ...

    @abstractmethod
    def hide(self) -> None: ...

    @abstractmethod
    def attach_command(self) -> str | None: ...

    @abstractmethod
    def close(self) -> None:
        """Owner closed before the lock is released."""

    @abstractmethod
    def session_status(self) -> SessionStatus: ...

    @abstractmethod
    def resume(self, *, mode: str | None = None, input: str | None = None) -> Record:
        """Even with a living idle owner."""
