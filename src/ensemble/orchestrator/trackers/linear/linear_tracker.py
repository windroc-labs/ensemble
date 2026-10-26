"""The engine Tracker on Linear, from a gql callable: its states mapped to IssueState, its reads and
writes in Linear's query text (a run's issue read included), and which of its comments and
attachments are a run's."""

import functools
import re
from typing import TypeVar

from ensemble.orchestrator.engine import config
from ensemble.orchestrator.engine.flow.handoff import parse_handoff
from ensemble.orchestrator.engine.flow.tracker import Tracker
from ensemble.orchestrator.engine.flow.types import (
    NO_ISSUE,
    UNREADABLE,
    Approved,
    ArchivePlan,
    Changes,
    Comment,
    Detail,
    Finished,
    Found,
    Group,
    Issue,
    IssueState,
    Link,
    Linked,
    Move,
    Note,
    Origin,
    Pick,
    Recheck,
    Ref,
    Snapshot,
    SubIssue,
)
from ensemble.orchestrator.engine.types import (
    MOVES,
    UNOWNED,
    UUID_RE,
    IssueError,
    LinearApp,
    LinearError,
    State,
    TrackerKind,
)
from ensemble.orchestrator.trackers.linear import ops
from ensemble.orchestrator.trackers.linear.ops import DONE, HISTORY, call, client_id, last_move
from ensemble.utils import LOCAL, one_line, parse_time

T = TypeVar("T")

# Last wins where two keys share an id, so todo and in_progress skip it as before.
STATES = {s: v for s, v in MOVES.items() if s not in (State.DONE, State.CANCELED)}

LISTED = {IssueState.READY: State.TODO, IssueState.RUNNING: State.IN_PROGRESS}

URL = "https://ensemble.invalid/run/"

Q_STATE_NAME = "query($i: String!) { workflowState(id: $i) { name } }"

STAMPS = "canceledAt completedAt"
Q_ISSUE = (
    "query($i: String!, $after: String) { issue(id: $i) { state { id type } updatedAt "
    + STAMPS
    + " "
    + HISTORY
    + " } }"
)
SNAPSHOT_BATCH = 50  # Q_FINISHED's page: issues with their history
Q_SNAPSHOTS = (
    "query($f: IssueFilter, $after: String) { issues(filter: $f, includeArchived: true, "
    f"first: {SNAPSHOT_BATCH}) {{ nodes {{ identifier updatedAt state {{ id type }} "
    + STAMPS
    + " "
    + HISTORY
    + " } } }"
)
Q_CHANGED = (
    "query($f: IssueFilter, $c: String) { issues(filter: $f, includeArchived: true, first: 100, "
    "after: $c, orderBy: updatedAt) { nodes { identifier updatedAt } "
    "pageInfo { hasNextPage endCursor } } }"
)
# children(first: 1): whether a sub-issue has its own, so a leaf costs no read
CHILD_PAGE = (
    "nodes { id identifier updatedAt state { name type } children(first: 1) { nodes { id } } } "
    "pageInfo { hasNextPage endCursor }"
)
Q_ARCHIVE = (
    "query($i: String!) { issue(id: $i) { id identifier url archivedAt updatedAt state { name type } "
    "children(first: 50) { " + CHILD_PAGE + " } } }"
)
Q_CHILDREN = (
    "query($i: String!, $c: String) { issue(id: $i) { children(first: 50, after: $c) { "
    + CHILD_PAGE
    + " } } }"
)
Q_FINISHED = (
    """query($t: ID, $d: [ID!], $s: [ID!], $a: [ID!], $c: String, $after: String) {
  issues(filter: { team: { id: { eq: $t } }, or: [{ state: { id: { in: $d } }, delegate: { id: { in: $a } } },
  { state: { id: { in: $s } } }] }, first: 50, after: $c) {
  nodes { id identifier updatedAt """
    + STAMPS
    + " "
    + HISTORY
    + """ }
  pageInfo { hasNextPage endCursor } } }"""
)
M_ARCHIVE = "mutation($i: String!) { issueArchive(id: $i) { success } }"
Q_HANDOFF = """query($t: ID, $s: ID, $a: [ID!]) { issues(filter: { team: { id: { eq: $t } }, state: { id: { eq: $s } },
  project: { null: false }, delegate: { id: { in: $a } } }, first: 100) {
  nodes { id identifier url title priority createdAt project { id name } delegate { id } attachments { nodes { title url } } } } }"""
Q_DETAIL = (
    """query($i: String!, $after: String) { issue(id: $i) { state { id }
  """
    + HISTORY
    + """
  comments(first: 250) { nodes { body createdAt user { id email name isMe } } }
  relations(first: 250) { nodes { relatedIssue { id } } }
  inverseRelations(first: 250) { nodes { issue { id } } } } }"""
)
Q_CHILD = """query($c: ID!) { issues(filter: { id: { eq: $c } }, includeArchived: true) { nodes { id identifier } } }"""
M_CREATE = "mutation($in: IssueCreateInput!) { issueCreate(input: $in) { success issue { id identifier } } }"
M_RELATE = (
    "mutation($in: IssueRelationCreateInput!) { issueRelationCreate(input: $in) { success } }"
)
M_TITLE = (
    "mutation($i: String!, $t: String!) { issueUpdate(id: $i, input: { title: $t }) { success } }"
)
M_ATTACH = "mutation($i: String!, $u: String!, $t: String) { attachmentLinkURL(issueId: $i, url: $u, title: $t) { success } }"
M_UNARCHIVE = "mutation($i: String!) { issueUnarchive(id: $i) { success } }"
Q_ATTACHED = "query($i: String!) { issue(id: $i) { attachments(first: 50) { nodes { url } } } }"
Q_ID = "query($i: String!) { issue(id: $i) { id team { id } } }"
M_UPDATE = (
    "mutation($c: String!, $b: String!) { commentUpdate(id: $c, input: { body: $b }) { success } }"
)
ATTACHED = ("already been linked", "Duplicate attachment", "Unable to create issue attachment")
RELATIONS = "inverseRelations(first: 50) { nodes { type issue { identifier state { type } } } }"
Q_RELATIONS = "query($i: String!) { issue(id: $i) { " + RELATIONS + " } }"
Q_RECHECK = (
    "query($i: String!) { issue(id: $i) { state { id } delegate { id } assignee { id email } project { id name } "
    "labels { nodes { id name parent { id } } } } }"
)
# ops.HISTORY's nodes with their actors: nested objects there would multiply Q_FINISHED's complexity
Q_HISTORY = (
    "query($i: String!, $after: String) { issue(id: $i) { "
    "history(first: 250, after: $after, orderBy: createdAt) { "
    "nodes { createdAt toStateId actor { id email isMe } botActor { type } } "
    "pageInfo { hasNextPage endCursor } } } }"
)
_NODES = (
    "nodes { identifier title description state { id name type } "
    "attachments(first: 20) { nodes { url } } comments(first: 1, orderBy: createdAt) { nodes { body } } }"
)
_FOUND = f"searchIssues(term: $t, filter: $f, first: 10, includeArchived: true) {{ {_NODES} }}"
_NAMED = f"issues(filter: $n, first: 50, includeArchived: true) {{ {_NODES} }}"
Q_SEARCH = f"query($t: String!, $f: IssueFilter) {{ {_FOUND} }}"
Q_SEARCH_IDS = f"query($t: String!, $f: IssueFilter, $n: IssueFilter) {{ {_FOUND} {_NAMED} }}"
Q_IDS = f"query($n: IssueFilter) {{ {_NAMED} }}"
Q_UPSTREAM = "query($i: String!) { issue(id: $i) { identifier description } }"
GROUP_BY_STATE = {
    State.DONE: Group.DONE,
    State.IN_REVIEW: Group.IN_REVIEW,
    State.IN_PROGRESS: Group.IN_PROGRESS,
    State.TODO: Group.TODO,
    State.CANCELED: Group.CANCELED,
}
GROUP_BY_TYPE = {
    "completed": Group.DONE,
    "started": Group.IN_PROGRESS,
    "unstarted": Group.TODO,
    "canceled": Group.CANCELED,
}
Q_READ = """query($i: String!) { issue(id: $i) { id identifier url title description createdAt
  project { id }
  comments(first: 250) { nodes { body createdAt user { id email name isMe } } }
  attachments(first: 50) { nodes { url } }
  labels(first: 50) { nodes { id } }
  relations(first: 50) { nodes { relatedIssue { identifier title state { name } } } }
  inverseRelations(first: 50) { nodes { issue { identifier title state { name } } } } } }"""


def q_issues(fields=""):
    return (
        (
            "query($f: IssueFilter, $c: String) { issues(filter: $f, first: 100, after: $c) { nodes { id identifier url priority createdAt "
            "project { id name } delegate { id } assignee { id email } "
        )
        + fields
        + " } pageInfo { hasNextPage endCursor } } }"
    )


def pick(nodes, group, chosen, kind):
    """The choice of the issue's labels in group (none when group is empty): chosen maps a label id
    to its name; kind names the labels, "task" or "Agent"."""
    found = [n for n in nodes if group and (n["parent"] or {}).get("id") == group]
    if not found:
        return Pick()
    if len(found) > 1:
        names = ", ".join(n["name"] for n in found)
        return Pick(
            error=f"Several {kind} labels ({names}); keep one, then move the issue back to Todo."
        )
    if (name := chosen.get(found[0]["id"])) is None:
        return Pick(
            error=f'{kind[0].upper()}{kind[1:]} label "{found[0]["name"]}" is not in {LOCAL}\'s '
            f"[orchestrator.linear.{kind.lower()}_labels]. Fix the label, then move the issue back to Todo."
        )
    return Pick(name)


def blockers(issue):
    nodes = [n for n in issue["inverseRelations"]["nodes"] if n["type"] == "blocks"]
    return [
        n["issue"]["identifier"] if n.get("issue") else UNREADABLE
        for n in nodes
        if not n.get("issue") or n["issue"]["state"]["type"] not in DONE
    ]


def is_record(attachment):
    # the URL alone identifies one: a source may be titled "Run rate analysis"
    return attachment["url"].startswith(URL)


def is_comment(comment):
    # isMe means the harness account because callers query with its token
    return (
        bool((comment["user"] or {}).get("isMe"))
        and re.match(f"Run {UUID_RE.pattern} · ", comment["body"]) is not None
    )


def finished_at(issue, finished):
    """The latest of issue's move into finished and its canceledAt and completedAt (the history
    read can lack a move into Duplicate that canceledAt holds); None: unknown."""
    times = [parse_time(issue[k]) for k in ("canceledAt", "completedAt") if issue[k]]
    if moved := last_move(issue["history"]["nodes"], finished):
        times.append(moved)
    return max(times, default=None)


def _quiet(*_, **__):
    pass


def _selves(roles):
    return frozenset(r.user_id for r in roles.values())


def _origin(user, selves) -> Origin:
    return "self" if user.get("isMe") or user.get("id") in selves else "other"


def _move_origin(node, selves) -> Origin:
    """Linear automation's (no actor, or a bot) is self."""
    if node["botActor"] or not node["actor"]:
        return "self"
    return _origin(node["actor"], selves)


class LinearTracker(Tracker):
    """states: {State key: state id}; team_id; finished: ops.Team.finished; roles: {user id: role};
    humans: human_members emails, subscribed on notify; selves: the role apps' user ids;
    say: caller's logger; missing method dependencies raise TypeError. identity and timeout bind
    gql; None leaves it as given. Comment refs are client id parts; home is a project.
    Origin self: harness (isMe), configured role app ids or automation; other: other users.
    Userless comments are omitted. task_group, agent_group: label groups; label_tasks,
    label_clients: {label id: task or client}, which recheck's picks read."""

    batch = SNAPSHOT_BATCH

    def __init__(
        self,
        gql,
        *,
        states=None,
        team_id=None,
        finished=None,
        roles=None,
        humans=(),
        selves=None,
        say=_quiet,
        identity=None,
        timeout=None,
        task_group=None,
        label_tasks=None,
        agent_group=None,
        label_clients=None,
    ):
        self.comment_namespace = (identity.user_id,) if isinstance(identity, LinearApp) else ()
        if identity is not None:
            bound = {"identity": identity} | ({} if timeout is None else {"timeout": timeout})
            gql = functools.partial(gql, **bound)
        self.gql, self.state_ids, self.team_id, self.roles = gql, states, team_id, roles
        self.finished_ids, self.humans, self.selves = finished, humans, selves
        self.say, self.hist = say, {}
        self.state_names: dict[IssueState, str] = {}
        self.task_group, self.label_tasks = task_group, label_tasks or {}
        self.agent_group, self.label_clients = agent_group, label_clients or {}
        self.users = {role: uid for uid, role in (roles or {}).items()}
        self.states = {states[k]: v for k, v in STATES.items()} if states else {}
        self.ids = {v: states[k] for k, v in MOVES.items()} if states else {}

    def state_name(self, state: IssueState) -> str:
        if state not in self.state_names:
            _need("states", self.state_ids)
            self.state_names[state] = self.gql(Q_STATE_NAME, i=self.ids[state])["workflowState"][
                "name"
            ]
        return self.state_names[state]

    def _state(self, sid):
        found = self.states.get(sid)
        if found not in (IssueState.READY, IssueState.RUNNING) and sid in self.finished_ids:
            return IssueState.DONE if sid == self.ids[IssueState.DONE] else IssueState.CANCELED
        return found

    @classmethod
    def connect(cls, gql, cfg, *, say):
        """Checks the team and task label group, then uses configured role app ids; keeps the task and
        Agent label maps. Calls gql as linear.harness."""
        linear = config.linear_settings(cfg, "router")
        gql = functools.partial(gql, identity=linear.harness)
        found = ops.team(gql, linear)
        ops.task_group(gql, linear)
        return cls(
            gql,
            states=found.states,
            team_id=found.id,
            finished=found.finished,
            roles=ops.role_ids(linear.roles),
            humans=linear.human_members,
            selves=_selves(linear.roles),
            say=say,
            task_group=linear.task_label_group,
            label_tasks={i: task for task, i in linear.task_labels.items()},
            agent_group=linear.agent_label_group,
            label_clients={i: client for client, i in linear.agent_labels.items()},
        )

    @classmethod
    def of_team(cls, gql, linear, team, roles):
        """From ops.team_and_roles' read."""
        return cls(
            gql,
            states=team.states,
            team_id=team.id,
            finished=team.finished,
            roles=roles,
            humans=linear.human_members,
            selves=_selves(linear.roles),
        )

    @classmethod
    def reader(cls, gql, linear):
        """A run's issue read, as linear.harness."""
        return cls(
            gql,
            states=linear.states,
            team_id=linear.team,
            selves=_selves(linear.roles),
            identity=linear.harness,
        )

    def role(self, issue):
        return _need("roles", self.roles).get((issue.get("delegate") or {}).get("id"))

    def assigned(self, state):
        return self._listed(state)

    def _listed(self, state, fields=""):
        """Page state's project issues, return the first 100 role delegates; an unowned Todo one
        logs claim-skip."""
        if state not in LISTED:
            raise ValueError(f"no list of {state} issues")
        flt = {
            "team": {"id": {"eq": _need("team_id", self.team_id)}},
            "project": {"null": False},
            "state": {"id": {"eq": _need("states", self.state_ids)[LISTED[state]]}},
        }
        found, cursors, variables = [], set(), {"f": flt}
        while True:
            page = self.gql(q_issues(fields), **variables)["issues"]
            for issue in page["nodes"]:
                if self.role(issue) is not None:
                    found.append(issue)
                elif state is IssueState.READY:
                    self.say(
                        "claim-skip",
                        issue["identifier"],
                        once=True,
                        reason=UNOWNED[TrackerKind.LINEAR],
                    )
            if not page["pageInfo"]["hasNextPage"]:
                return found[:100]
            variables["c"] = ops.advance("role issues", page["pageInfo"]["endCursor"], cursors)

    def ready(self):
        """Relations come with the list; when that read fails, a logged todo-error, the plain list
        and one read per issue, an unreadable one blocked by UNREADABLE."""
        try:
            issues = self._listed(IssueState.READY, RELATIONS)
        except LinearError as e:
            self.say("todo-error", error=one_line(e))
            issues = self._listed(IssueState.READY)
            for i in issues:
                try:
                    i.update(self.gql(Q_RELATIONS, i=i["id"])["issue"])
                except LinearError:
                    i["inverseRelations"] = {"nodes": [{"type": "blocks", "issue": None}]}
        return [(i, tuple(blockers(i))) for i in issues]

    def recheck(self, key):
        """ready: in the configured Todo state id, whatever other key shares it; refused without
        a project; no such issue: not ready."""
        try:
            found = self.gql(Q_RECHECK, i=key)["issue"]
        except LinearError as e:
            if not ops.not_found(e):
                raise
            found = None
        if found is None:
            return NO_ISSUE
        ready = found["state"]["id"] == _need("states", self.state_ids)[State.TODO]
        nodes = found["labels"]["nodes"]
        return Recheck(
            ready,
            role=self.role(found),
            refused=None if found["project"] else "no project",
            assignee=found["assignee"],
            home=found["project"],
            task=pick(nodes, self.task_group, self.label_tasks, "task"),
            client=pick(nodes, self.agent_group, self.label_clients, "Agent"),
        )

    def moved_at(self, key, state, *, by=None):
        """History read once per key."""
        selves = _need("selves", self.selves)
        if key not in self.hist:
            self.hist[key] = self.gql(Q_HISTORY, i=key)["issue"]["history"]["nodes"]
        moved = {_need("states", self.state_ids)[LISTED[state]]}
        keep = None if by is None else (lambda n: _move_origin(n, selves) == by)
        return last_move(self.hist[key], moved, keep)

    def read(self, ident):
        _need("states", self.state_ids)
        _need("finished", self.finished_ids)
        try:
            issue = self.gql(Q_ISSUE, i=ident)["issue"]
        except LinearError as e:
            if not ops.not_found(e):
                raise
            return None
        return self._snapshot(issue) if issue else None

    def snapshots(self, idents):
        """Omits what read must resolve: one read per SNAPSHOT_BATCH, by this team and number,
        lists no other team's (moved) or deleted issue."""
        _need("states", self.state_ids)
        _need("finished", self.finished_ids)
        team = _need("team_id", self.team_id)
        found = {}
        for at in range(0, len(idents), SNAPSHOT_BATCH):
            batch = idents[at : at + SNAPSHOT_BATCH]
            numbers = [int(i.rsplit("-", 1)[1]) for i in batch]
            f = {"team": {"id": {"eq": team}}, "number": {"in": numbers}}
            nodes = {n["identifier"]: n for n in self.gql(Q_SNAPSHOTS, f=f)["issues"]["nodes"]}
            found |= {i: self._snapshot(nodes[i]) for i in batch if i in nodes}
        return found

    def changed(self, since, pages):
        """Issues updated after since, archived too, newest first; issues None: past pages."""
        f = {
            "team": {"id": {"eq": _need("team_id", self.team_id)}},
            "updatedAt": {"gt": since.isoformat()},
        }
        found, cursor, cursors = {}, None, set()
        for _ in range(pages):
            page = self.gql(Q_CHANGED, f=f, c=cursor)["issues"]
            for n in page["nodes"]:
                at = parse_time(n["updatedAt"])
                found[n["identifier"]] = max(at, found.get(n["identifier"], at))
            if not page["pageInfo"]["hasNextPage"]:
                return Changes(found, max(found.values(), default=None))
            cursor = ops.advance("changed issues", page["pageInfo"]["endCursor"], cursors)
        return Changes(None, max(found.values(), default=None))

    def _snapshot(self, issue):
        """Another team's finished state by its type."""
        state = issue["state"]
        found = self._state(state["id"])
        if found is None and state["type"] in DONE:
            found = IssueState.DONE if state["type"] == "completed" else IssueState.CANCELED
        updated = parse_time(issue["updatedAt"])
        if found in (IssueState.DONE, IssueState.CANCELED):
            return Snapshot(found, finished_at(issue, self.finished_ids), updated)
        return Snapshot(found, last_move(issue["history"]["nodes"], {state["id"]}), updated)

    def issue(self, ident):
        selves = _need("selves", self.selves)
        node = self.gql(Q_READ, i=ident)["issue"]
        if node is None:
            raise IssueError(f"{ident}: issue not found")
        if node["identifier"] != ident:
            raise IssueError(f"Linear returned {str(node['identifier'])[:40]!r} for {ident}")
        notes = sorted(
            (
                Note(
                    c["body"],
                    c["createdAt"],
                    c["user"].get("email"),
                    c["user"].get("name"),
                    _origin(c["user"], selves),
                )
                for c in node["comments"]["nodes"]
                if c["user"] and not is_comment(c)
            ),
            key=lambda n: parse_time(n.at),
        )
        linked = {}
        for r in [x["relatedIssue"] for x in node["relations"]["nodes"]] + [
            x["issue"] for x in node["inverseRelations"]["nodes"]
        ]:
            linked.setdefault(
                r["identifier"], Linked(r["identifier"], r["title"], r["state"]["name"])
            )
        description = node["description"] or ""
        return Issue(
            id=node["id"],
            identifier=node["identifier"],
            url=node["url"],
            title=node["title"],
            description=description,
            handoff=parse_handoff(description),
            created_at=node["createdAt"],
            home=(node["project"] or {}).get("id"),
            notes=tuple(notes),
            links=tuple(Link(a["url"]) for a in node["attachments"]["nodes"]),
            linked=tuple(linked.values()),
            labels=tuple(n["id"] for n in node["labels"]["nodes"]),
        )

    def search(self, words, idents):
        if not words and not idents:
            return ()
        f = {"team": {"id": {"eq": _need("team_id", self.team_id)}}}
        n = {**f, "number": {"in": [int(i.rsplit("-", 1)[1]) for i in idents]}}
        if words and idents:
            data = self.gql(Q_SEARCH_IDS, t=" ".join(words), f=f, n=n)
        elif words:
            data = self.gql(Q_SEARCH, t=" ".join(words), f=f)
        else:
            data = self.gql(Q_IDS, n=n)
        nodes = [
            *data.get("searchIssues", {"nodes": []})["nodes"],
            *data.get("issues", {"nodes": []})["nodes"],
        ]
        by_id = {_need("states", self.state_ids)[k]: g for k, g in GROUP_BY_STATE.items()}
        return tuple(_found(n, by_id) for n in nodes)

    def description(self, ident):
        try:
            node = self.gql(Q_UPSTREAM, i=ident)["issue"]
        except LinearError as e:
            if not ops.not_found(e):
                raise
            return None
        return None if node is None else node["description"] or ""

    def finished(self, owners):
        states, team = _need("states", self.state_ids), _need("team_id", self.team_id)
        roles, ended = _need("roles", self.roles), _need("finished", self.finished_ids)
        ids = [uid for uid, role in roles.items() if role in owners]
        done = sorted(ended & {states[State.DONE]})
        nodes, cursor = [], None
        while True:
            page = self.gql(
                Q_FINISHED,
                t=team,
                d=done,
                s=sorted(ended.difference(done)),
                a=ids,
                c=cursor,
            )["issues"]
            nodes += page["nodes"]
            end = page["pageInfo"]["endCursor"]
            if not page["pageInfo"]["hasNextPage"] or not end or end == cursor:
                break
            cursor = end
        return [
            Finished(n["id"], n["identifier"], finished_at(n, ended), parse_time(n["updatedAt"]))
            for n in nodes
        ]

    def archive_plan(self, key):
        """Unless archived, its descendants level by level (Linear lists no archived sub-issue)."""
        try:
            node = self.gql(Q_ARCHIVE, i=key)["issue"]
        except LinearError as e:
            if not ops.not_found(e):
                raise
            raise LinearError(f"{key}: issue not found") from e
        if node is None:
            raise LinearError(f"{key}: issue not found")
        archived = node["archivedAt"] is not None
        found = tuple(map(_sub_issue, [node, *([] if archived else self._descendants(node))]))
        return ArchivePlan(
            node["id"],
            node["identifier"],
            node["url"],
            archived,
            found[1:],
            () if archived else tuple(s for s in found if not s.finished),
        )

    def _descendants(self, root):
        found, level, seen = [], [root], {root["id"]}
        while level:
            below = []
            for parent in level:
                if parent is not root and _leaf(parent):
                    continue
                for child in self._children(root, parent):
                    if child["id"] not in seen:
                        seen.add(child["id"])
                        below.append(child)
            found += below
            level = below
        return found

    def _children(self, root, parent):
        """Every page of parent's children; the root's first came with it."""
        first = parent["children"] if parent is root else self._children_page(parent, None)
        page, cursors = _checked(root, parent, first), set()
        nodes = list(page["nodes"])
        while page["pageInfo"]["hasNextPage"]:
            where = f"{parent['identifier']} children"
            after = ops.advance(where, page["pageInfo"]["endCursor"], cursors)
            page = _checked(root, parent, self._children_page(parent, after))
            nodes += page["nodes"]
        return nodes

    def _children_page(self, parent, after):
        return (self.gql(Q_CHILDREN, i=parent["id"], c=after)["issue"] or {}).get("children")

    def _write_archive(self, key):
        call(self.gql, M_ARCHIVE, "issueArchive", i=key)
        return True

    def approved(self, owners):
        _need("states", self.state_ids)
        roles = _need("roles", self.roles)
        nodes = self.gql(
            Q_HANDOFF,
            t=_need("team_id", self.team_id),
            s=self.ids[IssueState.APPROVED],
            a=[uid for uid, role in roles.items() if role in owners],
        )["issues"]["nodes"]
        return [
            Approved(
                n["id"],
                n["identifier"],
                n["url"],
                n["title"],
                n["priority"],
                n["createdAt"],
                n["project"]["id"],
                n["project"]["name"],
                roles[n["delegate"]["id"]],
                tuple(
                    (a["title"], a["url"]) for a in n["attachments"]["nodes"] if not is_record(a)
                ),
            )
            for n in nodes
        ]

    def detail(self, key):
        _need("states", self.state_ids)
        _need("finished", self.finished_ids)
        selves = _need("selves", self.selves)
        issue = self.gql(Q_DETAIL, i=key)["issue"]
        return Detail(
            self._state(issue["state"]["id"]),
            tuple(
                Move(h["createdAt"], self._state(h["toStateId"]), self._state(h["fromStateId"]))
                for h in issue["history"]["nodes"]
            ),
            tuple(
                Comment(
                    c["createdAt"],
                    c["body"],
                    c["user"]["name"],
                    _origin(c["user"], selves),
                    run=is_comment(c),
                )
                for c in issue["comments"]["nodes"]
                if c["user"]
            ),
            frozenset(r["relatedIssue"]["id"] for r in issue["relations"]["nodes"])
            | {r["issue"]["id"] for r in issue["inverseRelations"]["nodes"]},
        )

    def created(self, once):
        nodes = self.gql(Q_CHILD, c=client_id(*once))["issues"]["nodes"]
        return Ref(nodes[0]["id"], nodes[0]["identifier"]) if nodes else None

    def create(self, once, *, home, owner, priority, title, description):
        _need("states", self.state_ids)
        made = call(
            self.gql,
            M_CREATE,
            "issueCreate",
            **{
                "in": {
                    "id": client_id(*once),
                    "teamId": _need("team_id", self.team_id),
                    "projectId": home,
                    "delegateId": self.users[owner],
                    "assigneeId": None,
                    "stateId": self.ids[IssueState.READY],
                    "priority": priority,
                    "title": title,
                    "description": description,
                }
            },
        )["issue"]
        return Ref(made["id"], made["identifier"])

    def relate(self, key, other):
        call(
            self.gql,
            M_RELATE,
            "issueRelationCreate",
            **{"in": {"type": "related", "issueId": key, "relatedIssueId": other}},
        )

    def comment(self, key, body, ref=None):
        ops.comment(
            self.gql, key, body, None if ref is None else client_id(*self.comment_namespace, *ref)
        )

    def move(self, key, to, *, frm=None):
        _need("states", self.state_ids)
        if frm is not None:
            return ops.move(self.gql, key, self.ids[to], self.ids[frm])
        if self.gql(ops.Q_ISSUE_STATE, i=key)["issue"]["state"]["id"] != self.ids[to]:
            call(self.gql, ops.M_STATE, "issueUpdate", i=key, s=self.ids[to])
        return None

    def claim(self, key):
        _need("states", self.state_ids)
        call(self.gql, ops.M_STATE, "issueUpdate", i=key, s=self.ids[IssueState.RUNNING])

    def comment_and_move(self, key, body, *, to, frm, notify):
        _need("states", self.state_ids)
        return ops.comment_and_move(
            self.gql,
            key,
            body,
            to=self.ids[to],
            frm=self.ids[frm],
            emails=self.humans if notify else (),
        )

    def notify(self, key, people):
        return ops.subscribe(self.gql, key, people)

    def links(self, key):
        return {a["url"] for a in self.gql(Q_ATTACHED, i=key)["issue"]["attachments"]["nodes"]}

    def link(self, key, url, title):
        try:
            call(self.gql, M_ATTACH, "attachmentLinkURL", i=key, u=url, t=title)
        except LinearError as e:
            if not any(s in str(e) for s in ATTACHED):  # the GitHub integration links PRs itself
                raise

    def retitle(self, key, title):
        call(self.gql, M_TITLE, "issueUpdate", i=key, t=title)

    def on_board(self, ident):
        issue = self.gql(Q_ID, i=ident)["issue"]
        return issue["id"] if issue and issue["team"]["id"] == self.team_id else None

    def unarchive(self, key):
        call(self.gql, M_UNARCHIVE, "issueUnarchive", i=key)

    def find_comment(self, key, ref):
        return ops.find_comment(self.gql, client_id(*self.comment_namespace, *ref), key)

    def put_comment(self, key, body, ref):
        cid = client_id(*self.comment_namespace, *ref)
        try:
            done = self.gql(M_UPDATE, c=cid, b=body)["commentUpdate"]
        except LinearError as e:
            if not ops.not_found(e):
                raise
            done = None
        if done is None:
            done = self.gql(ops.M_COMMENT_ID, c=cid, i=key, b=body)["commentCreate"]
        return done["success"]


def _need(name: str, value: T | None) -> T:
    if value is None:
        raise TypeError(f"LinearTracker needs {name}")
    return value


def _sub_issue(node):
    state = node["state"]
    finished = state["type"] in DONE
    return SubIssue(
        node["id"], node["identifier"], state["name"], finished, parse_time(node["updatedAt"])
    )


def _leaf(node):
    return (node.get("children") or {}).get("nodes") == []


def _checked(root, parent, page):
    if ops.is_page(page):
        return page
    raise LinearError(
        f"archive {root['identifier']}: sub-issues of {parent['identifier']} unreadable "
        "(deleted or archived mid-read?): nothing archived"
    )


def _found(n, by_id):
    st, comments = n["state"], n["comments"]["nodes"]
    group = by_id.get(st["id"], GROUP_BY_TYPE.get(st["type"], Group.BACKLOG))
    return Found(
        n["identifier"],
        n["title"],
        st["name"],
        group,
        tuple(a["url"] for a in n["attachments"]["nodes"]),
        n["description"] or "",
        comments[0]["body"] if group == Group.CANCELED and comments else "",
    )
