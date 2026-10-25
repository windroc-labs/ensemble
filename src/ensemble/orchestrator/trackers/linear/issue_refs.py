"""Linear references for ensemble-orchestrator issue options: each resource's connection as an
issue_page source, and resolution of an option's value to the one entity it names in scope. A value
is an id or an exact name (a team also takes its key); no match, several or one out of scope raises
IssueError with the candidates and the lookup command that lists them, never guessing one.
"""

import re
import shlex

from ensemble.orchestrator.engine.flow.types import (
    ISSUE_ID,
)
from ensemble.orchestrator.engine.types import (
    UUID_RE,
    GraphQLErrors,
    IssueError,
    LinearError,
)
from ensemble.orchestrator.trackers.linear import issue_page
from ensemble.orchestrator.trackers.linear.client import messages

NOT_FOUND = "Entity not found"
CANDIDATES = 10
LOOKUP = "ensemble-orchestrator issue lookup"
TEAM = "id name key"
STATE = "id name type team { id }"
LABEL = "id name isGroup retiredAt team { id } parent { id }"
# teams bounded: at 50 (the default) a page of 250 projects passes Linear's 10,000-point limit
PROJECT = "id name teams(first: 10) { nodes { id } pageInfo { hasNextPage } }"
MILESTONE = "id name project { id }"
USER = "id name email active"
_PAGE = "$first: Int!, $after: String"
Q_TEAM = f"query($id: String!) {{ team(id: $id) {{ {TEAM} }} }}"
Q_LABEL = f"query($id: String!) {{ issueLabel(id: $id) {{ {LABEL} }} }}"
Q_MILESTONE = f"query($id: String!) {{ projectMilestone(id: $id) {{ {MILESTONE} }} }}"
SETTABLE = {"isGroup": {"eq": False}}
Q_ISSUE = "query($id: String!) { issue(id: $id) { id identifier } }"
Q_PROJECT = (
    "query($id: String!, $t: ID) { project(id: $id) { id name "
    "teams(filter: { id: { eq: $t } }) { nodes { id } } } }"
)


def found(gql, query, **variables):
    """query's data, or None when Linear finds no such entity."""
    try:
        return gql(query, **variables)
    except LinearError as error:
        if isinstance(error, GraphQLErrors):
            missing = any(m.startswith(NOT_FOUND) for m in messages(error.errors))
        else:
            missing = NOT_FOUND in str(error)
        if not missing:
            raise
        return None


def entity(gql, query, field, **variables):
    """data[field] of query, or None when Linear finds no such entity."""
    data = found(gql, query, **variables)
    return None if data is None else data[field]


def _node(node):
    return node, []


def every(*filters):
    """Filter all of filters (None ones skipped) hold; None when none is left."""
    parts = [f for f in filters if f]
    return parts[0] if len(parts) == 1 else {"and": parts} if parts else None


def named(text):
    """Filter of names holding text in any case; None (everything) for no text."""
    return {"name": {"containsIgnoreCase": text}} if text else None


def teams(match, shape=_node):
    """Source of the workspace's teams that match holds; lookup matches keys too."""
    selection = issue_page.connection("teams", "filter: $f", TEAM)
    return f"query($f: TeamFilter, {_PAGE}) {{ {selection} }}", "teams", {"f": match}, shape


def team_text(text):
    if not text:
        return None
    return {"or": [named(text), {"key": {"containsIgnoreCase": text}}]}


def states(team_id, match, shape=_node):
    selection = issue_page.connection("workflowStates", "filter: $f", STATE)
    query = f"query($f: WorkflowStateFilter, {_PAGE}) {{ {selection} }}"
    return query, "workflowStates", {"f": every({"team": {"id": {"eq": team_id}}}, match)}, shape


def labels(team_id, match, shape=_node):
    """Source of team_id's and the workspace's labels that match holds."""
    scope = {"or": [{"team": {"id": {"eq": team_id}}}, {"team": {"null": True}}]}
    selection = issue_page.connection("issueLabels", "filter: $f", LABEL)
    query = f"query($f: IssueLabelFilter, {_PAGE}) {{ {selection} }}"
    return query, "issueLabels", {"f": every(scope, match)}, shape


def projects(team_id, match, shape=_node):
    selection = issue_page.connection("projects", "filter: $f", PROJECT)
    query = f"query($t: String!, $f: ProjectFilter, {_PAGE}) {{ team(id: $t) {{ {selection} }} }}"
    return query, "team.projects", {"t": team_id, "f": match}, shape


def milestones(project_id, match, shape=_node):
    selection = issue_page.connection("projectMilestones", "filter: $f", MILESTONE)
    query = (
        f"query($p: String!, $f: ProjectMilestoneFilter, {_PAGE}) "
        f"{{ project(id: $p) {{ {selection} }} }}"
    )
    return query, "project.projectMilestones", {"p": project_id, "f": match}, shape


def users(team_id, match, shape=_node):
    """Source of team_id's members, or the workspace's users for None; disabled ones included."""
    if team_id is None:
        selection = issue_page.connection("users", "filter: $f, includeDisabled: true", USER)
        return f"query($f: UserFilter, {_PAGE}) {{ {selection} }}", "users", {"f": match}, shape
    selection = issue_page.connection("members", "filter: $f, includeDisabled: true", USER)
    query = f"query($t: String!, $f: UserFilter, {_PAGE}) {{ team(id: $t) {{ {selection} }} }}"
    return query, "team.members", {"t": team_id, "f": match}, shape


def user_text(text):
    if not text:
        return None
    return {"or": [named(text), {"email": {"containsIgnoreCase": text}}]}


def show(node):
    key = f" {node['key']}" if node.get("key") else ""
    return f"{node['id']}{key} {node['name']!r}"


def command(resource, value, *scope):
    """The lookup command listing resource within scope (option, value pairs) near value."""
    words = [*LOOKUP.split(), resource, *scope]
    if not UUID_RE.fullmatch(value):
        words += ["--query", value]
    return " ".join(shlex.quote(w) for w in words)


def resolve(gql, value, *, option, what, find, near, get, scope, lookup, usable=None):
    """The one node value names. find is the source of the exact matches, of which only the usable
    ones count when any is; near(text) the source of the candidates shown when none matches (None:
    any in scope). An id is read through get, (query, field, variables), and must satisfy scope;
    what names the resource and its scope in errors.
    """
    if UUID_RE.fullmatch(value):
        node = entity(gql, get[0], get[1], id=value, **get[2])
        if node is not None and scope(node):
            return node
        why = "no such " + what if node is None else f"{show(node)} is not a {what}"
        raise IssueError(
            f"{option} {value}: {why}; {_candidates(gql, near(None))}; list them: {lookup}"
        )
    found = [row[0] for row in issue_page.walk(gql, find)]
    found = [n for n in found if usable is None or usable(n)] or found
    if len(found) == 1:
        return found[0]
    if found:
        listed = ", ".join(show(n) for n in found)
        raise IssueError(
            f"{option} {value!r}: {len(found)} match, name one by id: {listed}; list them: {lookup}"
        )
    raise IssueError(
        f"{option} {value!r}: no {what} has that name; {_candidates(gql, near(value))}; "
        f"list them: {lookup}"
    )


def _candidates(gql, source):
    rows, more, _ = issue_page.fetch(gql, source, CANDIDATES)
    listed = ", ".join(show(row[0]) for row in rows) or "none"
    return f"candidates: {listed}" + (", …" if more else "")


def team(gql, value):
    """The team whose id, exact name or key (any case) is value."""
    return resolve(
        gql,
        value,
        option="--team",
        what="team",
        find=teams({"or": [{"name": {"eq": value}}, {"key": {"eqIgnoreCase": value}}]}),
        near=lambda text: teams(team_text(text)),
        get=(Q_TEAM, "team", {}),
        scope=lambda _: True,
        lookup=command("teams", value),
    )


def project(gql, team_id, value):
    """The project of team_id whose id or exact name is value."""
    return resolve(
        gql,
        value,
        option="--project",
        what=f"project of team {team_id}",
        find=projects(team_id, {"name": {"eq": value}}),
        near=lambda text: projects(team_id, named(text)),
        get=(Q_PROJECT, "project", {"t": team_id}),
        scope=lambda node: bool(node["teams"]["nodes"]),
        lookup=command("projects", value, "--team", team_id),
    )


def _current(node):
    return None if node["retiredAt"] else (node, [])


def label(gql, team_id, value, *, option="--label", retired=False):
    """The label of team_id or the workspace whose id or exact name is value; a label group is
    refused, as no issue can be given one, and so is a retired label unless retired, a current one
    of the same name still winning.
    """
    lookup = command("labels", value, "--team", team_id)
    node = resolve(
        gql,
        value,
        option=option,
        what=f"label of team {team_id} or the workspace",
        find=labels(team_id, {"name": {"eq": value}}),
        near=lambda text: labels(team_id, every(SETTABLE, named(text)), _current),
        usable=lambda node: not (node["isGroup"] or node["retiredAt"]),
        get=(Q_LABEL, "issueLabel", {}),
        scope=lambda node: node["team"] is None or node["team"]["id"] == team_id,
        lookup=lookup,
    )
    if node["isGroup"]:
        raise IssueError(
            f"{option} {value}: {show(node)} is a label group: name one of its labels; "
            f"list them: {lookup}"
        )
    if node["retiredAt"] and not retired:
        raise IssueError(f"{option} {value}: {show(node)} is retired; list them: {lookup}")
    return node


def milestone(gql, project_id, value):
    """The milestone of project_id whose id or exact name is value; no project is refused."""
    if project_id is None:
        raise IssueError(f"--milestone {value}: a milestone needs its --project")
    return resolve(
        gql,
        value,
        option="--milestone",
        what=f"milestone of project {project_id}",
        find=milestones(project_id, {"name": {"eq": value}}),
        near=lambda text: milestones(project_id, named(text)),
        get=(Q_MILESTONE, "projectMilestone", {}),
        scope=lambda node: node["project"]["id"] == project_id,
        lookup=command("milestones", value, "--project", project_id),
    )


def issue(gql, value, *, option="--parent"):
    """{id, identifier} of the issue whose identifier (TASK-1) or id is value."""
    if not (re.fullmatch(ISSUE_ID, value) or UUID_RE.fullmatch(value)):
        raise IssueError(
            f"{option} {value!r}: want an issue identifier such as TASK-1 or an issue id"
        )
    node = entity(gql, Q_ISSUE, "issue", id=value)
    if node is None:
        raise IssueError(f"{option} {value}: no such issue")
    return node
