"""ensemble-orchestrator issue label swap: replace one label of an issue by another in one update that
names only those two (addedLabelIds, removedLabelIds), so every other label stays; refused when the
issue carries neither, or the new one would join a label group the issue already holds another of.
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


def swap(gql, ident, old, new):
    """Replace the label old names on ident by the one new names, within the issue's team and the
    workspace.
    """
    data = issue_read.lookup(gql, Q_READ, ident)
    node, team_id = data["issue"], data["issue"]["team"]["id"]
    carried = carried_labels(gql, ident, node)
    gone = _old(gql, team_id, carried, old)
    added = issue_refs.label(gql, team_id, new, option="--to")
    ids = {n["id"] for n in carried}
    if gone["id"] not in ids and added["id"] not in ids:
        raise LabelRefused(
            f"label swap {ident} refused, nothing written: it carries neither --from {old!r} nor "
            f"--to {new!r}; its labels: ensemble-orchestrator issue read {ident}"
        )
    if gone["id"] not in ids or gone["id"] == added["id"]:
        return issue_write.receipt(node, "label swap", "unchanged", data["viewer"])
    group = (added["parent"] or {}).get("id")
    clash = [
        n
        for n in carried
        if group
        and (n["parent"] or {}).get("id") == group
        and n["id"] not in (gone["id"], added["id"])
    ]
    if added["id"] not in ids and clash:
        raise LabelRefused(
            f"label swap {ident} refused, nothing written: --to {new!r} is in label group "
            f"{group}, which the issue already holds "
            + ", ".join(issue_refs.show(n) for n in clash)
            + " of; swap that one instead"
        )
    variables = {"a": [] if added["id"] in ids else [added["id"]], "r": [gone["id"]]}
    issue_write.write(gql, "label swap", ident, M_SWAP, "issueUpdate", i=node["id"], **variables)
    return issue_write.receipt(node, "label swap", "changed", data["viewer"])


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


def _old(gql, team_id, carried, value):
    """The carried label whose id or exact name is value, else the label value names in scope,
    retired ones included: a label retired after the swap left the issue.
    """
    found = [n for n in carried if value in (n["id"], n["name"])]
    if len(found) == 1:
        return found[0]
    if found:
        listed = ", ".join(issue_refs.show(n) for n in found)
        raise IssueError(
            f"--from {value!r}: the issue carries {len(found)} labels of that name, name one by id: "
            f"{listed}; list them: {issue_refs.command('labels', value, '--team', team_id)}"
        )
    return issue_refs.label(gql, team_id, value, option="--from", retired=True)
