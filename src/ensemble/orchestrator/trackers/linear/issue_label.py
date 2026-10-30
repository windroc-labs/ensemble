"""ensemble-orchestrator issue label swap, add and remove: one label of an issue leaves, joins, or
both, in one update naming only those (addedLabelIds, removedLabelIds), so every other label stays;
refused when a swap's issue carries neither, or the joining label would join a label group the
issue already holds another of.
"""

from ensemble.orchestrator.engine.types import IssueError, LabelRefused, LinearError
from ensemble.orchestrator.trackers.linear import issue_read, issue_refs, issue_write, ops


def labels_page(after=""):
    return (
        f"labels(first: {issue_read.PAGE}{after}) {{ nodes {{ id name parent {{ id }} }} "
        "pageInfo { hasNextPage endCursor } }"
    )


Q_READ = (
    "query($i: String!) { viewer { id name } issue(id: $i) { id identifier url "
    f"team {{ id key }} {labels_page()} }} }}"
)
Q_MORE = f"query($i: String!, $c: String) {{ issue(id: $i) {{ {labels_page(', after: $c')} }} }}"
M_SWAP = (
    "mutation($i: String!, $a: [String!], $r: [String!]) { issueUpdate(id: $i, input: "
    "{ addedLabelIds: $a, removedLabelIds: $r }) { success } }"
)


# The options naming the label that leaves and the one that joins, by op.
OPTIONS = {"swap": ("--from", "--to"), "add": (None, "LABEL"), "remove": ("LABEL", None)}


def change(gql, op, ident, old=None, new=None):
    """op on ident's labels: the label old names leaves and the one new names joins, each within the
    issue's team and the workspace.
    """
    what, (leaving, joining) = f"label {op}", OPTIONS[op]
    data = issue_read.lookup(gql, Q_READ, ident)
    node, team_id = data["issue"], data["issue"]["team"]["id"]
    carried = carried_labels(gql, ident, node)
    gone = None if old is None else _old(gql, team_id, carried, old, leaving)["id"]
    added = None if new is None else issue_refs.label(gql, team_id, new, option=joining)
    ids = {n["id"] for n in carried}
    if op == "swap" and gone not in ids and added["id"] not in ids:
        raise LabelRefused(
            f"{what} {ident} refused, nothing written: it carries neither --from {old!r} nor "
            f"--to {new!r}; its labels: ensemble-orchestrator issue read {ident}"
        )
    joins = added is not None and added["id"] not in ids
    leaves = gone in ids and gone != (added or {}).get("id")
    if not (joins or leaves):
        return issue_write.receipt(node, what, "unchanged", data["viewer"])
    group = (added["parent"] or {}).get("id") if joins else None
    clash = [
        n
        for n in carried
        if group and (n["parent"] or {}).get("id") == group and n["id"] not in (gone, added["id"])
    ]
    if clash:
        raise LabelRefused(
            f"{what} {ident} refused, nothing written: {joining} {new!r} is in label group "
            f"{group}, which the issue already holds "
            + ", ".join(issue_refs.show(n) for n in clash)
            + " of; swap that one instead"
        )
    variables = {"a": [added["id"]] if joins else [], "r": [gone] if leaves else []}
    issue_write.write(gql, what, ident, M_SWAP, "issueUpdate", i=node["id"], **variables)
    return issue_write.receipt(node, what, "changed", data["viewer"])


def carried_labels(gql, ident, node):
    """The labels node (read with labels_page()) carries, every later page read too."""
    page = node["labels"]
    labels, seen = list(page["nodes"]), set()
    while page["pageInfo"]["hasNextPage"]:
        after = ops.advance(f"{ident} labels", page["pageInfo"]["endCursor"], seen)
        page = (gql(Q_MORE, i=node["id"], c=after)["issue"] or {}).get("labels")
        if not ops.is_page(page):
            raise LinearError(f"{ident}: labels page missing: deleted mid-read?")
        labels += page["nodes"]
    return labels


def _old(gql, team_id, carried, value, option):
    """The carried label whose id or exact name is value, else the label value names in scope,
    retired ones included: a label retired after the change left the issue.
    """
    found = [n for n in carried if value in (n["id"], n["name"])]
    if len(found) == 1:
        return found[0]
    if found:
        listed = ", ".join(issue_refs.show(n) for n in found)
        raise IssueError(
            f"{option} {value!r}: the issue carries {len(found)} labels of that name, name one by id: "
            f"{listed}; list them: {issue_refs.command('labels', value, '--team', team_id)}"
        )
    return issue_refs.label(gql, team_id, value, option=option, retired=True)
