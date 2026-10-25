"""Linear operations, comment identity and board configuration checks."""

import hashlib
import subprocess
import uuid
from dataclasses import dataclass
from typing import TypeGuard

from ensemble.orchestrator.engine.types import (
    FinishedType,
    LinearError,
    State,
    Unavailable,
)
from ensemble.utils import one_line, parse_time


@dataclass(frozen=True)
class Team:
    id: str
    name: str
    states: dict[str, str]
    finished: frozenset[str]


M_COMMENT = "mutation($i: String!, $b: String!) { commentCreate(input: { issueId: $i, body: $b }) { success } }"
M_COMMENT_ID = (
    "mutation($c: String!, $i: String!, $b: String!) { commentCreate(input: { id: $c, issueId: $i, body: $b }) "
    "{ success } }"
)
Q_COMMENT = "query($c: String!) { comment(id: $c) { id body issue { id } user { isMe } } }"
M_STATE = (
    "mutation($i: String!, $s: String!) { issueUpdate(id: $i, input: { stateId: $s }) { success } }"
)
M_SUBSCRIBE = (
    "mutation($i: String!, $e: String!) { issueSubscribe(id: $i, userEmail: $e) { success } }"
)
Q_ISSUE_STATE = "query($i: String!) { issue(id: $i) { state { id } } }"
# orderBy createdAt returns newest first, so the latest moves are on this page.
HISTORY = (
    "history(first: 250, after: $after, orderBy: createdAt) { "
    "nodes { createdAt actorId fromStateId toStateId } pageInfo { hasNextPage endCursor } }"
)
NOT_FOUND = "Entity not found"
DONE = {"completed", "canceled", "duplicate"}  # Linear state types an issue is finished in


def is_page(page) -> TypeGuard[dict]:
    """Whether page is a connection page: nodes and pageInfo."""
    return (
        isinstance(page, dict)
        and isinstance(page.get("nodes"), list)
        and isinstance(page.get("pageInfo"), dict)
    )


def advance(where, end, seen):
    """end, the cursor of the page after one claiming more, added to seen; LinearError when it is
    empty or already read, which would read the same page forever.
    """
    if not end or end in seen:
        raise LinearError(f"{where}: hasNextPage without a new endCursor ({end!r})")
    seen.add(end)
    return end


def call(gql, query, field, **v):
    out = gql(query, **v)[field]
    if not out["success"]:
        raise LinearError(f"{field}: success: false")
    return out


def client_id(*parts):
    return str(uuid.UUID(bytes=hashlib.sha256("/".join(parts).encode()).digest()[:16], version=4))


def not_found(e):
    return isinstance(e, LinearError) and NOT_FOUND in str(e)


def comment(gql, issue, body, cid=None):
    if cid is None:
        call(gql, M_COMMENT, "commentCreate", i=issue, b=body)
    else:
        call(gql, M_COMMENT_ID, "commentCreate", c=cid, i=issue, b=body)


def find_comment(gql, cid, issue_id):
    try:
        found = gql(Q_COMMENT, c=cid)["comment"]
    except LinearError as e:
        if not_found(e):
            return None
        raise
    if found is None:
        return None
    if (found.get("issue") or {}).get("id") != issue_id or not (found.get("user") or {}).get(
        "isMe"
    ):
        raise LinearError(f"comment {cid} is not ours")
    return found["body"]


def subscribe(gql, issue, emails):
    notes = ""
    for email in emails:
        try:
            call(gql, M_SUBSCRIBE, "issueSubscribe", i=issue, e=email)
        except (LinearError, Unavailable, OSError, subprocess.SubprocessError) as e:
            notes += f"\n\nCould not subscribe {email}: {one_line(e)}"
    return notes


def move(gql, issue, to, frm):
    now = gql(Q_ISSUE_STATE, i=issue)["issue"]["state"]["id"]
    if now != frm:
        return now
    call(gql, M_STATE, "issueUpdate", i=issue, s=to)
    return None


def comment_and_move(gql, issue, body, *, to, frm, emails=()):
    # move first: a failed or skipped move posts no comment, so a retry never repeats it
    notes = subscribe(gql, issue, emails)
    left = move(gql, issue, to, frm)
    if left is None:
        comment(gql, issue, body + notes)
    return left


def last_move(nodes, states, keep=None):
    return max(
        (
            parse_time(n["createdAt"])
            for n in nodes
            if n["toStateId"] in states and (keep is None or keep(n))
        ),
        default=None,
    )


Q_USER = "query($e: String!) { users(filter: { email: { eqIgnoreCase: $e } }) { nodes { id } } }"


def user_id(gql, email):
    nodes = gql(Q_USER, e=email)["users"]["nodes"]
    return nodes[0]["id"] if nodes else None


def role_ids(roles):
    """{configured app user id: role}; no app credentials or email lookup needed."""
    return {app.user_id: name for name, app in roles.items()}


TEAMS = """teams(filter: { id: { eq: $t } }) { nodes { id name
  states(first: 100) { nodes { id type } } } }"""
Q_TEAM = "query($t: ID) { " + TEAMS + " }"


def team(gql, linear):
    return _team(gql(Q_TEAM, t=linear.team)["teams"]["nodes"], linear)


def team_and_roles(gql, linear):
    """Read the team as the harness; role ids come solely from configuration."""
    return team(gql, linear), role_ids(linear.roles)


def _team(nodes, linear):
    if not nodes:
        raise LinearError(
            f"orchestrator/config.toml: linear.team {linear.team} not found in Linear"
        )
    t = nodes[0]
    states = linear.states
    ids = {s["id"] for s in t["states"]["nodes"]}
    if bad := [f"{k} {states[k]}" for k in State if states[k] not in ids]:
        raise LinearError(
            f"orchestrator/config.toml: [linear.states] not workflow states of team {t['name']!r}: {', '.join(bad)}"
        )
    finished = {states[State.DONE], states[State.CANCELED]}
    finished |= {
        s["id"]
        for s in t["states"]["nodes"]
        if s["type"] in (FinishedType.CANCELED, FinishedType.DUPLICATE)
    }
    return Team(t["id"], t["name"], {k: states[k] for k in State}, frozenset(finished))


Q_TASK_GROUP = (
    "query($i: String!) { issueLabel(id: $i) { isGroup children(first: 250) { nodes { id } } } }"
)


def task_group(gql, linear):
    # children are unpaginated (250): beyond that a valid id fails
    group = linear.task_label_group
    try:
        label = gql(Q_TASK_GROUP, i=group)["issueLabel"]
    except LinearError as e:
        raise LinearError(
            f"orchestrator/config.toml: linear.task_label_group {group} not found in Linear: {e}"
        ) from None
    if not label["isGroup"]:
        raise LinearError(
            f"orchestrator/config.toml: linear.task_label_group {group} is not a label group"
        )
    ids = {c["id"] for c in label["children"]["nodes"]}
    if bad := [f"{task} {i}" for task, i in linear.task_labels.items() if i not in ids]:
        raise LinearError(
            f"orchestrator/config.toml: [linear.task_labels] not labels of linear.task_label_group {group}: {', '.join(bad)}"
        )
