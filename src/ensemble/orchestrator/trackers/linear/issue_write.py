"""ensemble-orchestrator issue move and comment: one write each, never retried, and a receipt."""

from ensemble.orchestrator.engine.types import Receipt, Unavailable, WriteUnknown
from ensemble.orchestrator.trackers.linear import issue_names, issue_read, ops

Q_MOVE = (
    "query($i: String!) { viewer { id name } issue(id: $i) { id identifier url state { id } "
    "team { id key } } }"
)
Q_COMMENT = "query($i: String!) { viewer { id name } issue(id: $i) { id identifier url } }"
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
    if node["state"]["id"] == target:
        return receipt(node, "move", "unchanged", data["viewer"])
    write(gql, "move", ident, ops.M_STATE, "issueUpdate", i=node["id"], s=target)
    return receipt(node, "move", "changed", data["viewer"])


def comment(gql, ident, body):
    """Post body as a new top-level comment on ident."""
    data = issue_read.lookup(gql, Q_COMMENT, ident)
    node = data["issue"]
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
