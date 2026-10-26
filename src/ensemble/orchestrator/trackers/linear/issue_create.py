"""ensemble-orchestrator issue create: plan resolves every reference before anything is written; create
makes the issue in the configured team with only the given fields (backlog unless --state; --role
the delegate) and no assignee, then each blocking relation. Creating the issue is never retried; a relation that fails leaves the issue
made, and RelationsFailed names the failed ones.
"""

import shlex
import subprocess

from ensemble.orchestrator.engine.flow.types import (
    BOTH_DIRECTIONS,
    PRIORITY_NUMBERS,
    IssueState,
)
from ensemble.orchestrator.engine.types import (
    Blocking,
    GraphQLErrors,
    IssueError,
    LinearError,
    RelationsFailed,
    Unavailable,
    WriteUnknown,
)
from ensemble.orchestrator.trackers.linear import issue_names, issue_refs, issue_write, ops
from ensemble.orchestrator.trackers.linear.client import messages

M_CREATE = (
    "mutation($in: IssueCreateInput!) { issueCreate(input: $in) "
    "{ success issue { id identifier url } } }"
)
M_RELATE = (
    "mutation($in: IssueRelationCreateInput!) { issueRelationCreate(input: $in) { success } }"
)
Q_VIEWER = "query { viewer { id name } }"


def plan(gql, new, linear):
    """new resolved as (IssueCreateInput, [(option, {id, identifier}) of each relation], viewer);
    IssueError for any value that does not resolve, before anything is written; abstract names
    first.
    """
    team = linear.team
    fields = {"teamId": team, "title": new.title, "description": new.body}
    fields["stateId"] = issue_names.state_id(linear, new.state or IssueState.BACKLOG)
    labels = [] if new.task is None else [issue_names.task_label(linear, new.task)]
    if new.client is not None:
        labels.append(issue_names.client_label(linear, new.client))
    who = None if new.role is None else issue_names.delegate(linear, new.role)
    labels += (issue_refs.label(gql, team, value)["id"] for value in dict.fromkeys(new.labels))
    if labels:
        fields["labelIds"] = list(dict.fromkeys(labels))
    project = None if new.project is None else issue_refs.project(gql, team, new.project)
    if project is not None:
        fields["projectId"] = project["id"]
    if new.milestone is not None:
        project_id = None if project is None else project["id"]
        fields["projectMilestoneId"] = issue_refs.milestone(gql, project_id, new.milestone)["id"]
    if new.parent is not None:
        fields["parentId"] = issue_refs.issue(gql, new.parent)["id"]
    # else Linear makes the caller the assignee
    fields["assigneeId"] = None
    if who is not None:
        fields["delegateId"] = who
    if new.priority is not None:
        fields["priority"] = PRIORITY_NUMBERS[new.priority]
    relations = _relations(gql, {"--blocks": new.blocks, "--blocked-by": new.blocked_by})
    return fields, relations, gql(Q_VIEWER)["viewer"]


def _relations(gql, given):
    """[(option, issue), …] of each distinct issue given names; IssueError for one in both."""
    picked = {}
    for option, values in given.items():
        for value in dict.fromkeys(values):
            node = issue_refs.issue(gql, value, option=option)
            first, _ = picked.setdefault(node["id"], (option, node))
            if first != option:
                raise IssueError(BOTH_DIRECTIONS.format(identifier=node["identifier"]))
    return list(picked.values())


def create(gql, fields, relations, viewer):
    """Create the issue of fields, then each relation plan gave; its receipt, or RelationsFailed."""
    try:
        node = ops.call(gql, M_CREATE, "issueCreate", **{"in": fields})["issue"]
    except Unavailable as error:
        raise WriteUnknown(
            f"create: result unknown ({error}): look for it with ensemble-orchestrator issue "
            f"list --query={shlex.quote(fields['title'])} before creating it again"
        ) from None
    done, failed = [], []
    for option, other in relations:
        blocker, blocked = (node, other) if option == "--blocks" else (other, node)
        relation = Blocking("blocks" if option == "--blocks" else "blocked_by", other["identifier"])
        variables = {"type": "blocks", "issueId": blocker["id"], "relatedIssueId": blocked["id"]}
        try:
            ops.call(gql, M_RELATE, "issueRelationCreate", **{"in": variables})
        except (LinearError, Unavailable, OSError, subprocess.SubprocessError) as error:
            failed.append((relation, _reason(error)))
        else:
            done.append(relation)
    receipt = issue_write.receipt(node, "create", "changed", viewer)
    if failed:
        raise RelationsFailed(receipt, tuple(done), tuple(failed))
    return receipt


def _reason(error):
    if isinstance(error, GraphQLErrors):
        return "; ".join(messages(error.errors))
    if isinstance(error, Unavailable):
        return f"result unknown ({error})"
    return str(error) or type(error).__name__
