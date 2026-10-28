"""The engine's board port."""

from abc import ABC, abstractmethod
from collections.abc import Callable, Collection, Sequence
from datetime import datetime
from typing import Literal, Protocol

from ensemble.orchestrator.engine.flow.types import (
    Approved,
    ArchivePlan,
    ArchiveRefused,
    Changes,
    Detail,
    Finished,
    Found,
    Issue,
    IssueRecord,
    IssueState,
    Origin,
    Recheck,
    Ref,
    Snapshot,
)

Listed = Literal[IssueState.READY, IssueState.RUNNING]


class Tracker(ABC):
    batch = 1  # snapshots' idents per call

    @abstractmethod
    def state_name(self, state: IssueState) -> str: ...

    @abstractmethod
    def read(self, ident: str) -> Snapshot | None:
        """None: no such issue."""

    def snapshots(self, idents: Sequence[str]) -> dict[str, Snapshot | None]:
        return {i: self.read(i) for i in idents}

    def changed(self, since: datetime, pages: int) -> Changes:
        return Changes(None, None)

    @abstractmethod
    def issue(self, ident: str) -> Issue:
        """Raises the adapter's error: none, or another's."""

    @abstractmethod
    def search(self, words: Sequence[str], idents: Sequence[str]) -> tuple[Found, ...]:
        """Archived too; idents: this board's."""

    @abstractmethod
    def description(self, ident: str) -> str | None: ...

    @abstractmethod
    def finished(self, owners: Collection[str]) -> list[Finished]:
        """Owners' done, all canceled; read every page before archiving."""

    @abstractmethod
    def archive_plan(self, key: str) -> ArchivePlan:
        """Gone or unreadable raises the adapter's error."""

    @abstractmethod
    def _write_archive(self, key: str) -> bool:
        """With its sub-issues; False: archived meanwhile."""

    def archive(self, key: str, plan: ArchivePlan | None = None) -> bool:
        """With plan: writes only. False: archived."""
        plan = plan or self.archive_plan(key)
        if plan.outcome == "refused":
            raise ArchiveRefused(plan)
        if plan.outcome == "unchanged":
            return False
        return self._write_archive(plan.key)

    @abstractmethod
    def approved(self, owners: Collection[str]) -> list[Approved]:
        """Filed in a home, the adapter's grouping."""

    @abstractmethod
    def detail(self, key: str) -> Detail: ...

    @abstractmethod
    def created(self, once: tuple[str, ...]) -> Ref | None:
        """Archived too."""

    @abstractmethod
    def create(
        self,
        once: tuple[str, ...],
        *,
        home: str,
        owner: str,
        priority: int,
        title: str,
        description: str,
        task: str | None = None,
    ) -> Ref:
        """READY."""

    @abstractmethod
    def relate(self, key: str, other: str) -> None: ...

    @abstractmethod
    def comment(self, key: str, body: str, ref: tuple[str, ...] | None = None) -> None: ...

    @abstractmethod
    def move(self, key: str, to: IssueState, *, frm: IssueState | None = None) -> str | None:
        """No frm: from any state; None: moved, else its state, opaque."""

    @abstractmethod
    def claim(self, key: str) -> None:
        """Unread → RUNNING; caller rechecked."""

    @abstractmethod
    def comment_and_move(
        self, key: str, body: str, *, to: IssueState, frm: IssueState, notify: bool
    ) -> str | None: ...

    @abstractmethod
    def notify(self, key: str, people: Collection[str]) -> str:
        """Notes for failures."""

    @abstractmethod
    def assign(self, key: str, user: str) -> None: ...

    @abstractmethod
    def links(self, key: str) -> set[str]: ...

    @abstractmethod
    def link(self, key: str, url: str, title: str) -> None: ...

    @abstractmethod
    def retitle(self, key: str, title: str) -> None: ...

    @abstractmethod
    def on_board(self, ident: str) -> str | None:
        """Its key on this board."""

    @abstractmethod
    def unarchive(self, key: str) -> None: ...

    @abstractmethod
    def find_comment(self, key: str, ref: tuple[str, ...]) -> str | None: ...

    @abstractmethod
    def put_comment(self, key: str, body: str, ref: tuple[str, ...]) -> bool:
        """False: refused."""

    @abstractmethod
    def role(self, issue: IssueRecord) -> str: ...

    @abstractmethod
    def assigned(self, state: Listed) -> list[IssueRecord]: ...

    @abstractmethod
    def ready(self) -> list[tuple[IssueRecord, tuple[str, ...]]]: ...

    def dependency_cycles(self) -> tuple[tuple[str, ...], ...]:
        """Unfinished cycles, if inspectable."""
        return ()

    @abstractmethod
    def moved_at(self, key: str, state: Listed, *, by: Origin | None = None) -> datetime | None: ...

    @abstractmethod
    def recheck(self, key: str) -> Recheck: ...


class Connect(Protocol):
    def __call__(self, transport, cfg, /, *, say: Callable[..., None]) -> Tracker: ...
