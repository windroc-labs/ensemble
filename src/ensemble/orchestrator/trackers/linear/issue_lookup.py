"""ensemble-orchestrator issue lookup RESOURCE: the issue_page source listing a resource's entities with
their fixed fields, every same-named one its own item; labels only those an issue can carry.
"""

from ensemble.orchestrator.engine.types import IssueError, Resource
from ensemble.orchestrator.trackers.linear import issue_refs


def source(gql, query, team_id):
    """The source of query.resource matching query.query, in query.team (resolved) else team_id."""
    resource, text = query.resource, query.query
    if query.team is not None and resource is Resource.TEAMS:
        raise IssueError(f"lookup {resource} takes no --team")
    if (query.project is None) == (resource is Resource.MILESTONES):
        raise IssueError(
            "lookup milestones needs --project VALUE"
            if resource is Resource.MILESTONES
            else f"lookup {resource} takes no --project"
        )
    if resource is Resource.TEAMS:
        return issue_refs.teams(issue_refs.team_text(text), _team)
    team = issue_refs.team(gql, query.team)["id"] if query.team is not None else None
    if resource is Resource.USERS:
        return issue_refs.users(team, issue_refs.user_text(text), _user)
    team = team or team_id
    named = issue_refs.named(text)
    if resource is Resource.STATES:
        return issue_refs.states(team, named, _state)
    if resource is Resource.LABELS:
        applicable = issue_refs.every(issue_refs.SETTABLE, named)
        return issue_refs.labels(team, applicable, _label)
    if resource is Resource.PROJECTS:
        return issue_refs.projects(team, named, _project)
    project = issue_refs.project(gql, team, query.project)["id"]
    return issue_refs.milestones(project, named, _milestone)


def _team(node):
    return {"id": node["id"], "name": node["name"], "key": node["key"]}, []


def _state(node):
    return {"id": node["id"], "name": node["name"], "type": node["type"]}, []


def _label(node):
    if node["isGroup"] or node["retiredAt"]:
        return None
    item = {
        "id": node["id"],
        "name": node["name"],
        "team_id": node["team"]["id"] if node["team"] else None,
        "group_id": node["parent"]["id"] if node["parent"] else None,
    }
    return item, []


def _project(node):
    teams = node["teams"]
    item = {"id": node["id"], "name": node["name"], "team_ids": [t["id"] for t in teams["nodes"]]}
    return item, ["team_ids"] if teams["pageInfo"]["hasNextPage"] else []


def _milestone(node):
    return {"id": node["id"], "name": node["name"], "project_id": node["project"]["id"]}, []


def _user(node):
    item = {"id": node["id"], "name": node["name"], "email": node["email"]}
    return {**item, "active": node["active"]}, []
