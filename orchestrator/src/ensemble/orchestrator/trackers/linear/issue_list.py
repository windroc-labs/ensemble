"""ensemble-orchestrator issue list: the issue_page source of the configured team's issues that every
given filter holds, most recently updated first; archived ones only with --include-archived.
"""

from ensemble.orchestrator.engine.flow.types import PRIORITY_NUMBERS
from ensemble.orchestrator.trackers.linear import issue_names, issue_page, issue_refs

LABELS = 50
# Nested labels multiply the issue page's cost; 50 issues stay below Linear's 10,000 points.
PAGE = 50
FIELDS = (
    "id identifier title url state { id name } assignee { id name } project { id name } "
    f"parent {{ id }} labels(first: {LABELS}) {{ nodes {{ id name }} pageInfo {{ hasNextPage }} }}"
)
_SELECTION = issue_page.connection(
    "issues", "filter: $f, orderBy: updatedAt, includeArchived: $a", FIELDS
)
Q_LIST = f"query($f: IssueFilter, $a: Boolean, $first: Int!, $after: String) {{ {_SELECTION} }}"


def source(gql, query, linear):
    """The source of the configured team's issues that query's filters select."""
    team = linear.team
    match: list[dict] = [{"team": {"id": {"eq": team}}}]
    if query.query:
        text = {"containsIgnoreCase": query.query}
        match.append({"or": [{"title": text}, {"description": text}]})
    if query.state is not None:
        match.append({"state": _id(issue_names.state_id(linear, query.state))})
    if query.role is not None:
        who = issue_names.assignee(gql, linear, query.role)
        match.append({"assignee": {"null": True} if who is None else _id(who)})
    if query.task is not None:
        match.append({"labels": {"some": _id(issue_names.task_label(linear, query.task))}})
    if query.client is not None:
        match.append({"labels": {"some": _id(issue_names.client_label(linear, query.client))}})
    if query.priority is not None:
        match.append({"priority": {"eq": PRIORITY_NUMBERS[query.priority]}})
    if query.label is not None:
        match.append({"labels": {"some": _id(issue_refs.label(gql, team, query.label)["id"])}})
    if query.project is not None:
        match.append({"project": _id(issue_refs.project(gql, team, query.project)["id"])})
    if query.parent is not None:
        match.append({"parent": _id(issue_refs.issue(gql, query.parent)["id"])})
    variables = {"f": issue_refs.every(*match), "a": query.include_archived}
    return Q_LIST, "issues", variables, _item


def _id(found):
    return {"id": {"eq": found}}


def _item(node):
    labels, parent = node["labels"], node["parent"]
    item = {key: node[key] for key in ("id", "identifier", "title", "url", "state")}
    item.update(labels=labels["nodes"], assignee=node["assignee"], project=node["project"])
    item["parent_id"] = parent["id"] if parent else None
    return item, ["labels"] if labels["pageInfo"]["hasNextPage"] else []
