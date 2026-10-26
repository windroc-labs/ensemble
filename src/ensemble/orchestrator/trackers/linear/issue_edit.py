"""ensemble-orchestrator issue edit: one issueUpdate naming only what changes among title, description,
delegate (--role, which also clears the assignee), project, task, client and priority; a task or client replaces the issue's labels of that
group, every other label staying. Every given value already so: unchanged, nothing written.
"""

from ensemble.orchestrator.engine.flow.types import PRIORITY_NUMBERS
from ensemble.orchestrator.trackers.linear import (
    issue_label,
    issue_names,
    issue_read,
    issue_refs,
    issue_write,
)

Q_READ = (
    "query($i: String!) { viewer { id name } issue(id: $i) { id identifier url title description "
    f"priority assignee {{ id }} delegate {{ id }} project {{ id }} team {{ id key }} "
    f"{issue_label.labels_page()} }} }}"
)
M_EDIT = (
    "mutation($i: String!, $in: IssueUpdateInput!) { issueUpdate(id: $i, input: $in) { success } }"
)


def edit(gql, linear, change):
    """Change change.id's title, description, delegate, project, task, client and priority as
    given."""
    who = None if change.role is None else issue_names.delegate(linear, change.role)
    groups = {}
    if change.task is not None:
        groups["--task"] = (issue_names.task_label(linear, change.task), linear.task_label_group)
    if change.client is not None:
        groups["--client"] = (
            issue_names.client_label(linear, change.client),
            linear.agent_label_group,
        )
    project = None
    if change.project is not None:
        project = issue_refs.project(gql, linear.team, change.project)["id"]
    data = issue_read.lookup(gql, Q_READ, change.id)
    node = data["issue"]
    update = {}
    if change.title is not None and change.title != node["title"]:
        update["title"] = change.title
    if change.body is not None and change.body != node["description"]:
        update["description"] = change.body
    priority = None if change.priority is None else PRIORITY_NUMBERS[change.priority]
    if priority is not None and priority != node["priority"]:
        update["priority"] = priority
    if change.role is not None:
        if who != (node["delegate"] or {}).get("id"):
            update["delegateId"] = who
        if node["assignee"] is not None:
            update["assigneeId"] = None
    if project is not None and project != (node["project"] or {}).get("id"):
        update["projectId"] = project
    if scoped := [*groups, "--project"] if project is not None else list(groups):
        issue_names.in_team(linear, node, " and ".join(scoped))
    if groups:
        update.update(_labels(issue_label.carried_labels(gql, change.id, node), groups.values()))
    if not update:
        return issue_write.receipt(node, "edit", "unchanged", data["viewer"])
    issue_write.write(gql, "edit", change.id, M_EDIT, "issueUpdate", i=node["id"], **{"in": update})
    return issue_write.receipt(node, "edit", "changed", data["viewer"])


def _labels(carried, targets):
    """addedLabelIds and removedLabelIds putting each target label in place of its group's others."""
    ids = {n["id"] for n in carried}
    added = [label for label, _ in targets if label not in ids]
    removed = [
        n["id"]
        for label, group in targets
        for n in carried
        if (n["parent"] or {}).get("id") == group and n["id"] != label
    ]
    return {
        key: found
        for key, found in (("addedLabelIds", added), ("removedLabelIds", removed))
        if found
    }
