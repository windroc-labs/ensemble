"""The Linear orchestrator's data types used by two or more of its modules."""

import os
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal

from ensemble import delegate
from ensemble.orchestrator.engine.flow.tracker import Connect, Tracker
from ensemble.orchestrator.engine.flow.types import ISSUE_ID, Found, IssueState, Priority, Snapshot
from ensemble.utils import Stopped as Stopped

ClientSource = Literal["label", "default"]


@dataclass(frozen=True)
class Role:
    tasks: tuple[str, ...]
    max_runs: int
    run: delegate.RunConfig


@dataclass(frozen=True)
class Docs:
    repo: str
    branch: str
    dirs: Mapping[str, str]
    host: str = "github.com"

    def local_out(self, role: str, ident: str, sid: str) -> str | None:
        directory = self.dirs.get(role, "")
        return (
            os.path.join(directory, f"document-{ident}-{sid}.md")
            if os.path.isabs(directory)
            else None
        )


class TrackerKind(StrEnum):
    """[orchestrator] tracker: where the board's issues live."""

    LINEAR = "linear"
    LOCAL = "local"


# an issue's role owner: Linear reads its delegate, a local board its assignee directory
UNOWNED = MappingProxyType(
    {TrackerKind.LINEAR: "no configured role delegate", TrackerKind.LOCAL: "not assigned to a role"}
)
OWNERSHIP = MappingProxyType(
    {TrackerKind.LINEAR: "delegated to a role app", TrackerKind.LOCAL: "assigned to a role"}
)


class AppKind(StrEnum):
    """Which of ensemble's Linear OAuth apps: its token request's scopes."""

    HARNESS = "harness"
    ROLE = "role"
    MANAGER = "manager"


SCOPES = MappingProxyType(
    {
        AppKind.HARNESS: "read,write",
        AppKind.ROLE: "read,write,app:assignable,app:mentionable",
        AppKind.MANAGER: "read,write,app:assignable",
    }
)


@dataclass(frozen=True)
class LinearApp:
    """A Linear OAuth app: name in messages and logs, table its config table (fix messages),
    secret_service and token_service the Keychain services of its client secret and token record."""

    name: str
    kind: AppKind
    table: str
    client_id: str
    secret_service: str
    token_service: str
    user_id: str
    renewal: str = "transport"

    @property
    def scopes(self) -> str:
        return SCOPES[self.kind]

    @property
    def service(self) -> str:
        """The Keychain service of what its calls send: its token record's."""
        return self.token_service


@dataclass(frozen=True)
class LinearSettings:
    """[orchestrator.linear]: a Linear board's entities and keys, by id."""

    team: str
    states: Mapping[str, str]
    human_members: tuple[str, ...]
    harness: LinearApp
    # unset keys are ""; calls take it from config.manager_app
    manager: LinearApp
    task_label_group: str
    task_labels: Mapping[str, str]
    agent_label_group: str
    agent_labels: Mapping[str, str]
    project_repos: Mapping[str, str]
    roles: Mapping[str, LinearApp]


@dataclass(frozen=True)
class Config:
    task_tiers: Mapping[str, int]
    client_max_runs: Mapping[str, int]
    usage_limit: Mapping[str, bool]
    roles: Mapping[str, Role]
    local_clones: Mapping[str, str]
    tracker: TrackerKind
    linear: LinearSettings | None  # None: tracker = "local"
    board: str | None  # the local board's realpath; None for Linear
    work_dir: str
    docs: Docs
    stages: Mapping[str, int]
    delegate_kind: str
    delegates: Mapping[str, type]
    core: Mapping
    local_prefix: str

    def __post_init__(self):
        if (self.linear is None) != (self.tracker is TrackerKind.LOCAL):
            raise ValueError(
                f"tracker {self.tracker}: linear is set exactly when the tracker is linear"
            )

    @property
    def delegate_cls(self) -> type:
        return self.delegates[self.delegate_kind]

    def repo_host(self, role: str) -> str:
        return (self.roles[role].run.output.get("host") or self.docs.host).lower()

    @property
    def workdirs(self) -> str:
        return os.path.join(self.work_dir, "work")

    @property
    def logs_dir(self) -> str:
        return os.path.join(self.work_dir, "logs")

    @property
    def runs_log(self) -> str:
        return os.path.join(self.logs_dir, "runs.jsonl")

    def issue_workdir(self, issue: str) -> str:
        return os.path.join(self.workdirs, issue)

    def task_tier(self, task: str | None) -> int | None:
        """The [task_tiers] tier of task, else None: the run keeps its Delegate tier."""
        return None if task is None else self.task_tiers.get(task)


@dataclass(frozen=True)
class Writers:
    """A run's write-back side of its tracker: tracker writes as the run's role, harness posts the
    session comment; states: each State's value as Tracker.move returns it; humans: who In Review
    notifies (lowercase); repos: project → owner/name."""

    tracker: Tracker
    harness: Tracker
    states: Mapping[str, str]
    humans: tuple[str, ...]
    repos: Mapping[str, str]


@dataclass(frozen=True)
class Claim:
    """The run's start context; role selects its current configured write-back identity."""

    issue: str
    sid: str
    role: str
    uuid: str
    project: str | None
    target: tuple[str, str] | None
    client: str
    delegate_kind: str


@dataclass(frozen=True)
class Wiring:
    """One tracker kind's engine wiring, built by cli.py; transport: the Linear gql callable (a
    local board ignores it)."""

    connect: Connect  # the router's board
    reader: Callable[[Any, Config], Tracker]  # a run's issue read and Related-work search
    writers: Callable[[Any, Config, LinearApp | None], Writers]  # write-back writers
    claimed: Callable[[Any, Config, Claim], Writers]
    identity: Callable[[Config, str], LinearApp | None]  # a role's; None: none to check
    board: Callable[[Any, Config], Callable[[Any], Tracker]]
    # (cfg, src, *, dry): command src's token renewal policy; dry: show each app's token, renew none
    credentials: Callable[..., None]


class State(StrEnum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    HANDOFF = "handoff"
    DONE = "done"
    CANCELED = "canceled"
    BACKLOG = "backlog"


# each State's IssueState
MOVES = {
    State.BACKLOG: IssueState.BACKLOG,
    State.IN_REVIEW: IssueState.REVIEW,
    State.HANDOFF: IssueState.APPROVED,
    State.TODO: IssueState.READY,
    State.IN_PROGRESS: IssueState.RUNNING,
    State.DONE: IssueState.DONE,
    State.CANCELED: IssueState.CANCELED,
}


class Resource(StrEnum):
    """What ensemble-orchestrator issue lookup lists."""

    TEAMS = "teams"
    STATES = "states"
    LABELS = "labels"
    PROJECTS = "projects"
    MILESTONES = "milestones"
    USERS = "users"


@dataclass(frozen=True)
class ListQuery:
    """issue list's filters."""

    query: str | None
    state: IssueState | None
    role: str | None
    task: str | None
    client: str | None
    priority: Priority | None
    parent: str | None
    include_archived: bool
    label: str | None
    project: str | None


@dataclass(frozen=True)
class LookupQuery:
    """issue lookup's query."""

    resource: Resource
    query: str | None
    team: str | None
    project: str | None


@dataclass(frozen=True)
class NewIssue:
    """issue create's fields."""

    title: str
    body: str
    state: IssueState | None
    role: str | None
    task: str | None
    client: str | None
    priority: Priority | None
    parent: str | None
    labels: tuple[str, ...]
    project: str | None
    milestone: str | None
    blocks: tuple[str, ...]
    blocked_by: tuple[str, ...]


@dataclass(frozen=True)
class IssueEdit:
    """issue edit's fields."""

    id: str
    title: str | None
    body: str | None
    role: str | None
    project: str | None
    task: str | None
    client: str | None
    priority: Priority | None


class FinishedType(StrEnum):
    """Linear state types finished beside the configured done state: Duplicate is canceled-type."""

    CANCELED = "canceled"
    DUPLICATE = "duplicate"


class ConfigError(Exception):
    def __init__(self, msg: str, logs_dir: str | None = None):
        super().__init__(msg)
        self.msg, self.logs_dir = msg, logs_dir


class LinearError(Exception):
    pass


class KeychainError(LinearError):
    """A Keychain item, or an app's token renewal, left no usable credential; identity: the app's
    name when it is an app's."""

    def __init__(self, msg: str, identity: str | None = None):
        super().__init__(msg)
        self.identity = identity


class GraphQLErrors(LinearError):
    """A response's GraphQL errors, kept as .errors (client.messages gives each one's text)."""

    def __init__(self, errors):
        super().__init__(f"linear api error: {errors}")
        self.errors = errors


class WriteUnknown(LinearError):
    """A write Linear never answered: it may or may not have landed."""


class Unavailable(Exception):
    # its text never holds the request, headers or key
    def __init__(self, op, *, status=None, reason=None):
        super().__init__(f"{op}: {reason}" if reason is not None else f"{op}: HTTP {status}")
        self.op, self.status, self.reason = op, status, reason


class IssueError(LookupError):
    pass


class TrackerFileError(Exception):
    """A tracker file that cannot be read."""

    def __init__(self, path: str, reason: str):
        super().__init__(f"{path}: {reason}")
        self.path, self.reason = path, reason


class LabelRefused(Exception):
    """A label swap the issue's current labels block; nothing was written."""


@dataclass(frozen=True)
class Receipt:
    id: str
    identifier: str
    url: str
    operation: str
    result: Literal["changed", "unchanged"]
    actor_id: str
    actor_name: str
    comment_id: str | None = None


@dataclass(frozen=True)
class Blocking:
    direction: Literal["blocks", "blocked_by"]
    other: str


class RelationsFailed(Exception):
    """create made receipt's issue but not each failed relation (with its uncut error)."""

    def __init__(
        self, receipt: Receipt, done: tuple[Blocking, ...], failed: tuple[tuple[Blocking, str], ...]
    ):
        super().__init__(receipt.identifier)
        self.receipt, self.done, self.failed = receipt, done, failed


TRACKER_ERRORS = (LinearError, IssueError, TrackerFileError, OSError, subprocess.SubprocessError)


class InputError(RuntimeError):
    pass


@dataclass(frozen=True)
class Ref:
    """A first-claim search's doc or merged PR; owner: the Found it belongs to, if any."""

    label: str
    url: str
    owner: str | None


@dataclass(frozen=True)
class Related:
    issues: tuple[Found, ...] = ()
    refs: tuple[Ref, ...] = ()


@dataclass(frozen=True)
class Invalid:
    reason: str
    mapped: bool = False
    line: Literal["Repo", "Ref"] = "Repo"  # the description line the bounce says to fix


@dataclass(frozen=True)
class Transient:
    reason: str


@dataclass(frozen=True)
class Target:
    owner: str
    name: str
    branch: str = ""
    clone: str = ""
    host: str = "github.com"
    ref: str = ""  # a checked Ref: line's branch or tag


KEYCHAIN_NAME = re.compile(r"[A-Za-z0-9._@+][A-Za-z0-9._@+-]*")  # safe on a security -i line
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@dataclass(frozen=True, kw_only=True)
class Spec:
    """A run the router claimed or resumes: its issue's identity, its role (the tracker's for its
    owner: Linear's delegate, a local board's assignee), its session and client."""

    uuid: str
    issue: str
    project: str | None
    role: str | None
    sid: str
    task: str | None = None
    mode: str
    client: str
    client_source: ClientSource = "default"

    @classmethod
    def from_issue(
        cls, issue, *, role, mode, sid, task, client, client_source: ClientSource = "default"
    ):
        return cls(
            uuid=issue["id"],
            issue=issue["identifier"],
            project=(issue.get("project") or {}).get("id"),
            role=role,
            sid=sid,
            task=task,
            mode=mode,
            client=client,
            client_source=client_source,
        )

    def identity_error(self) -> str | None:
        if not re.fullmatch(ISSUE_ID, self.issue) or not UUID_RE.fullmatch(self.sid):
            return f"bad issue or session id: {self.issue} {self.sid}"
        return None

    def role_error(
        self, role: str | None, tasks: tuple[str, ...], tracker: TrackerKind
    ) -> str | None:
        if role is None:
            return UNOWNED[tracker]
        if self.task is not None and self.task not in tasks:
            return f"task {self.task!r} is not one of {role}'s tasks ({', '.join(tasks)})"
        return None


@dataclass(frozen=True)
class Entry:
    snapshot: Snapshot | None
    read: datetime


@dataclass(frozen=True)
class Refusal:
    versions: Mapping[str, datetime | None]  # the candidate's and each unfinished sub-issue's
    reason: str
    read: datetime


@dataclass
class PruneState:
    """prune.json; an Entry's snapshot None: gone; unbatched: read one by one."""

    synced: datetime | None = None
    issues: dict[str, Entry] = field(default_factory=dict)
    refused: dict[str, Refusal] = field(default_factory=dict)
    unbatched: set[str] = field(default_factory=set)
