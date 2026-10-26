"""ensemble-orchestrator issue move and comment: one write each (a move of an archived issue to an
active state unarchives it first), never retried, and a receipt."""

from ensemble.orchestrator.engine.flow.types import IssueState
from ensemble.orchestrator.engine.types import IssueError, Receipt, Unavailable, WriteUnknown
from ensemble.orchestrator.trackers.linear import issue_names, issue_read, ops
from ensemble.orchestrator.trackers.linear.linear_tracker import M_UNARCHIVE

Q_MOVE = (
    "query($i: String!) { viewer { id name } issue(id: $i) { id identifier url archivedAt "
    "state { id } team { id key } } }"
)
Q_COMMENT = (
    "query($i: String!) { viewer { id name } issue(id: $i) { id identifier url archivedAt } }"
)
M_COMMENT = (
    "mutation($i: String!, $b: String!) { commentCreate(input: { issueId: $i, body: $b }) "
    "{ success comment { id } } }"
)


def move(gql, linear, ident, state):
    """Move ident, an issue of the configured team, to the abstract state's configured id."""
    data = issue_read.lookup(gql, Q_MOVE, ident)
    node = data["issue"]
    issue_names.in_team(linear, node, "move")
    target = issue_names.state_id(linear, state)
    restore = node["archivedAt"] is not None and state not in (IssueState.DONE, IssueState.CANCELED)
    if restore:  # Linear's lists, the router's Todo list included, leave an archived issue out
        write(gql, "move", ident, M_UNARCHIVE, "issueUnarchive", i=node["id"])
    if node["state"]["id"] == target:
        return receipt(node, "move", "changed" if restore else "unchanged", data["viewer"])
    write(gql, "move", ident, ops.M_STATE, "issueUpdate", i=node["id"], s=target)
    return receipt(node, "move", "changed", data["viewer"])


def comment(gql, ident, body):
    """Post body as a new top-level comment on ident; an archived issue takes none."""
    data = issue_read.lookup(gql, Q_COMMENT, ident)
    node = data["issue"]
    if node["archivedAt"] is not None:
        raise IssueError(
            f"{ident}: archived, and Linear takes no comment on an archived issue: "
            f"ensemble-orchestrator issue move {ident} STATE, to any state but done or canceled, "
            "unarchives it"
        )
    out = write(gql, "comment", ident, M_COMMENT, "commentCreate", i=node["id"], b=body)
    comment_id = (out.get("comment") or {}).get("id")
    return receipt(node, "comment", "changed", data["viewer"], comment_id=comment_id)


def write(gql, op, ident, query, field, **variables):
    """The mutation's result; WriteUnknown when Linear never answered it."""
    try:
        return ops.call(gql, query, field, **variables)
    except Unavailable as error:
        raise WriteUnknown(
            f"{op} {ident}: result unknown ({error}): "
            f"check with ensemble-orchestrator issue read {ident} before retrying"
        ) from None


def receipt(node, op, result, actor, comment_id=None):
    ident, url = node["identifier"], node["url"]
    return Receipt(node["id"], ident, url, op, result, actor["id"], actor["name"], comment_id)
