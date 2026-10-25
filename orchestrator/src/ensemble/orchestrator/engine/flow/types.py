"""Tracker and workflow types."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum, StrEnum
from types import MappingProxyType
from typing import Any, Literal

UNREADABLE = "(unreadable)"
IssueRecord = dict[str, Any]  # opaque until 第 2 阶段
# self: the engine; other: user or manager
Origin = Literal["self", "other"]


class IssueState(StrEnum):
    BACKLOG = "backlog"
    READY = "ready"
    RUNNING = "running"
    REVIEW = "review"
    APPROVED = "approved"
    DONE = "done"
    CANCELED = "canceled"


class Priority(StrEnum):
    NONE = "none"
    URGENT = "urgent"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# a priority as an issue stores it
PRIORITY_NUMBERS = MappingProxyType(
    {Priority.NONE: 0, Priority.URGENT: 1, Priority.HIGH: 2, Priority.MEDIUM: 3, Priority.LOW: 4}
)
COMMENTS, COMMENT_CHARS, DESCRIPTION_CHARS, COLUMN = 5, 300, 600, 10


@dataclass(frozen=True)
class Snapshot:
    """state None: outside the machine; since: finished at, else last moved into its tracker state;
    None: unknown."""

    state: IssueState | None
    since: datetime | None
    updated: datetime | None = None


@dataclass(frozen=True)
class SubIssue:
    """key: archive's handle; ident: read's KEY-N; state: its name."""

    key: str
    ident: str
    state: str
    finished: bool
    updated: datetime | None = None


@dataclass(frozen=True)
class ArchivePlan:
    """unfinished: it first; descendants: any depth, none if archived."""

    key: str
    ident: str
    url: str
    archived: bool
    descendants: tuple[SubIssue, ...]
    unfinished: tuple[SubIssue, ...]

    @property
    def outcome(self) -> Literal["unchanged", "refused", "changed"]:
        if self.archived:
            return "unchanged"
        return "refused" if self.unfinished else "changed"


class ArchiveRefused(Exception):
    def __init__(self, plan: ArchivePlan):
        super().__init__()
        self.plan = plan


@dataclass(frozen=True)
class Finished:
    """key, ident: as SubIssue's; since: None when unknown."""

    key: str
    ident: str
    since: datetime | None
    updated: datetime | None = None


@dataclass(frozen=True)
class Changes:
    issues: Mapping[str, datetime] | None
    latest: datetime | None


@dataclass(frozen=True)
class Ref:
    key: str
    ident: str


@dataclass(frozen=True)
class Approved:
    """Times; sources omit run records; empty url: ID-only source."""

    key: str
    ident: str
    url: str
    title: str
    priority: int
    created: str
    home_key: str
    home_name: str
    owner: str
    sources: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Move:
    at: str
    to: IssueState | None
    frm: IssueState | None


@dataclass(frozen=True)
class Comment:
    """run: a run's."""

    at: str
    body: str
    author: str
    origin: Origin
    run: bool


@dataclass(frozen=True)
class Detail:
    state: IssueState | None
    moves: tuple[Move, ...]
    comments: tuple[Comment, ...]
    related: frozenset[str]


@dataclass(frozen=True)
class Pick:
    """None, None: no choice; error: unusable, its bounce comment; beats name."""

    name: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class Recheck:
    """ready: still READY; role: the assignee's, None if unassigned or not a role's; refused: None,
    or why not claimable, a skip reason shown as-is; assignee, home: opaque, for the claim; task,
    client: the picks."""

    ready: bool
    role: str | None
    refused: str | None
    assignee: IssueRecord | None
    home: IssueRecord | None
    task: Pick
    client: Pick


NO_ISSUE = Recheck(
    ready=False, role=None, refused=None, assignee=None, home=None, task=Pick(), client=Pick()
)


@dataclass(frozen=True)
class Stage:
    """See workflow.toml's header."""

    role: str
    input: Literal["research", "design", "build"]
    next: str
    require_instructions: bool
    title_prefix: str
    retitle: bool
    start: str
    done: str
    question: str
    failed: str
    first_failure: Literal["ready", "review"]
    approve: bool
    files: bool
    hint: str


ISSUE_ID = r"[A-Z][A-Z0-9]*-\d+"


IDENT_ERROR = "{ident!r}: want an issue identifier such as TASK-1 or an issue id"
BOTH_DIRECTIONS = (
    "{identifier}: given to both --blocks and --blocked-by; "
    "an issue cannot both block the new one and be blocked by it"
)


@dataclass(frozen=True)
class Note:
    body: str
    at: str
    account: str | None
    name: str | None
    origin: Origin


@dataclass(frozen=True)
class Link:
    url: str


@dataclass(frozen=True)
class Linked:
    identifier: str
    title: str
    state: str


@dataclass(frozen=True)
class Handoff:
    source: str
    instructions: str
    comments: str


@dataclass(frozen=True)
class Issue:
    id: str
    identifier: str
    url: str
    title: str
    description: str
    created_at: str
    home: str | None
    notes: tuple[Note, ...]
    links: tuple[Link, ...]
    linked: tuple[Linked, ...]

    handoff: Handoff | None = field(kw_only=True)
    labels: tuple[str, ...] = field(default=(), kw_only=True)


class Group(IntEnum):
    """Related work's rank: its state's place in the section."""

    DONE = 0
    IN_REVIEW = 1
    IN_PROGRESS = 2
    TODO = 3
    BACKLOG = 4
    CANCELED = 5


@dataclass(frozen=True)
class Found:
    """A first-claim search hit; last_comment only when Canceled."""

    identifier: str
    title: str
    state: str
    group: Group
    urls: tuple[str, ...]
    description: str
    last_comment: str
