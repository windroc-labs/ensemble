"""ensemble-orchestrator issue archive: the ident checked, then Tracker.archive over its plan."""

from ensemble.orchestrator.engine.types import Unavailable, WriteUnknown
from ensemble.orchestrator.trackers.linear import issue_read, issue_write

Q_ISSUE = "query($i: String!) { viewer { id name } issue(id: $i) { id identifier url } }"


def archive(tracker, gql, ident):
    """The receipt; ArchiveRefused when ident or a descendant is unfinished."""
    data = issue_read.lookup(gql, Q_ISSUE, ident)
    node = data["issue"]
    plan = tracker.archive_plan(node["id"])
    try:
        changed = tracker.archive(node["id"], plan)
    except Unavailable as error:  # the plan was read: only the write can be unanswered
        raise WriteUnknown(
            f"archive {ident}: result unknown ({error}): "
            f"check with ensemble-orchestrator issue read {ident} before retrying"
        ) from None
    result = "changed" if changed else "unchanged"
    return issue_write.receipt(node, "archive", result, data["viewer"])


def plan(tracker, gql, ident):
    return tracker.archive_plan(issue_read.lookup(gql, Q_ISSUE, ident)["issue"]["id"])
