"""ensemble-orchestrator issue's abstract names (states, roles, tasks, clients, priorities) as the
configured Linear ids; IssueError for a name the configuration lacks, before anything is written.
"""

from ensemble.orchestrator.engine.flow.types import IssueState
from ensemble.orchestrator.engine.types import MOVES, IssueError
from ensemble.orchestrator.trackers.linear import ops

Q_VIEWER = "query { viewer { id } }"
_KEYS = {state: key for key, state in MOVES.items()}


def state_id(linear, state: IssueState) -> str:
    """The id of the [linear.states] key state is."""
    return linear.states[_KEYS[state]]


def assignee(gql, linear, spec) -> str | None:
    """The user --role spec names: a role's account, me (the manager_key account) or unassigned."""
    if spec in linear.roles:
        account = linear.roles[spec].account
        if (found := ops.user_id(gql, account)) is None:
            raise IssueError(f"--role {spec}: account {account} not found in Linear")
        return found
    if spec == "me":
        return gql(Q_VIEWER)["viewer"]["id"]
    if spec == "unassigned":
        return None
    raise IssueError(f"--role {spec}: not a role: {', '.join([*linear.roles, 'me', 'unassigned'])}")


def task_label(linear, name) -> str:
    return _label("--task", name, linear.task_labels, "[orchestrator.linear.task_labels]")


def client_label(linear, name) -> str:
    if not linear.agent_label_group:
        raise IssueError(
            f"--client {name}: client labels are not configured "
            "([orchestrator.linear] agent_label_group)"
        )
    return _label("--client", name, linear.agent_labels, "[orchestrator.linear.agent_labels]")


def _label(option, name, labels, table):
    if name in labels:
        return labels[name]
    if not labels:
        raise IssueError(f"{option} {name}: none configured ({table})")
    raise IssueError(f"{option} {name}: not a name of {table}: {', '.join(labels)}")


def in_team(linear, node, option):
    """IssueError unless node, an issue read with team { id key }, is of the configured team."""
    if node["team"]["id"] != linear.team:
        raise IssueError(
            f"{node['identifier']} is in team {node['team']['key']}: {option} takes only issues "
            f"of the configured team (linear.team {linear.team})"
        )
