"""Issue backend over the broker's worker-only board and token files."""

import json
import os
from datetime import UTC, datetime

from ensemble.orchestrator.engine.types import (
    SCOPES,
    AppKind,
    IssueError,
    LinearApp,
    LinearSettings,
)
from ensemble.orchestrator.trackers.linear.client import graphql
from ensemble.orchestrator.trackers.linear.issues import LinearIssues

DIRECTORY = "/run/ensemble/secrets/linear"


def _file(name):
    with open(f"{DIRECTORY}/{name}.json", encoding="utf-8") as file:
        return json.load(file)


def _gql(query, **variables):
    try:
        token = _file("token")
        secret = token["access_token"]
        expiry = datetime.fromisoformat(token["expires_at"].replace("Z", "+00:00"))
        if (
            not isinstance(secret, str)
            or not secret
            or not secret.isascii()
            or any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in secret)
        ):
            raise ValueError
        if expiry.tzinfo is None or expiry <= datetime.now(UTC):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise IssueError(
            "Worker tracker access is missing, invalid or expired; ask the manager to restore access or send the current description and comments."
        ) from None
    return graphql(query, secret=secret, **variables)


def _app(name, user_id=""):
    return LinearApp(name, AppKind.ROLE, "worker", "", "", "", user_id)


def backend(command):
    try:
        os.stat(DIRECTORY)
    except FileNotFoundError:
        return None
    except OSError:
        raise IssueError(
            "Worker tracker settings cannot be read; ask the manager to restore access."
        ) from None
    try:
        board = _file("board")
        scopes = board["scopes"]
        if scopes not in ("read", SCOPES[AppKind.ROLE]):
            raise ValueError
        roles = {name: _app(name, user) for name, user in board["role_users"].items()}
        linear = LinearSettings(
            team=board["team"],
            states=board["states"],
            human_members=tuple(board["human_members"]),
            assignee_id="",
            harness=_app("harness"),
            manager=_app("manager"),
            task_label_group=board["task_label_group"],
            agent_label_group=board["agent_label_group"],
            task_labels=board["labels"]["task_labels"],
            agent_labels=board["labels"]["agent_labels"],
            project_repos=board["project_repos"],
            roles=roles,
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise IssueError(
            "Worker tracker settings are missing or invalid; ask the manager to restore access or send the current description and comments."
        ) from None
    if scopes == "read" and command not in ("read", "list", "lookup"):
        raise IssueError("Worker tracker access is read-only; the manager performs board writes.")
    return LinearIssues(_gql, linear)
