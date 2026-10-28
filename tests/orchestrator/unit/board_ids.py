import atexit
import hashlib
import os
import shutil
import tempfile
import uuid
from importlib.resources import files

from ensemble.orchestrator.engine.types import AppKind, LinearApp
from ensemble.orchestrator.trackers.linear import ops

TEAM = "00000000-0000-4000-8000-000000000001"
STATES = {
    k: f"00000000-0000-4000-8000-0000000000{i:02d}"
    for i, k in enumerate(
        ("todo", "in_progress", "in_review", "handoff", "done", "canceled", "backlog"), start=11
    )
}
# Not a config state: prune finds it by its type.
DUPLICATE = "00000000-0000-4000-8000-000000000018"
TYPES = {
    **dict(
        zip(
            STATES.values(),
            ("unstarted", "started", "started", "started", "completed", "canceled", "backlog"),
            strict=True,
        )
    ),
    DUPLICATE: "canceled",
}
FINISHED = frozenset({STATES["done"], STATES["canceled"], DUPLICATE})
TASK_GROUP = "00000000-0000-4000-8000-000000000002"
HARNESS_USER = "00000000-0000-4000-8000-000000000201"
MANAGER_USER = "00000000-0000-4000-8000-000000000202"
HARNESS_APP = {
    "client_id": "client-harness",
    "secret_service": "linear-app-harness-secret",
    "token_service": "linear-app-harness-token",
    "user_id": HARNESS_USER,
}
HARNESS_IDENTITY = LinearApp("harness", AppKind.HARNESS, "linear.harness", **HARNESS_APP)
HARNESS_LINE = "linear.harness = {{ {} }}\n".format(
    ", ".join(f'{k} = "{v}"' for k, v in HARNESS_APP.items())
)
MANAGER_APP = {
    "client_id": "client-manager",
    "secret_service": "linear-app-manager-secret",
    "token_service": "linear-app-manager-token",
    "user_id": MANAGER_USER,
}
MANAGER_IDENTITY = LinearApp("manager", AppKind.MANAGER, "linear.manager", **MANAGER_APP)
NO_MANAGER = LinearApp("manager", AppKind.MANAGER, "linear.manager", "", "", "", "")
MANAGER_LINE = "linear.manager = {{ {} }}\n".format(
    ", ".join(f'{k} = "{v}"' for k, v in MANAGER_APP.items())
)
HEADER = (
    'tracker = "linear"\n'
    + 'linear.team = "{}"\n{}linear.states = {{ {} }}\n'.format(
        TEAM, HARNESS_LINE, ", ".join(f'{k} = "{v}"' for k, v in STATES.items())
    )
    + 'linear.reader = { client_id = "client-reader", secret_service = "linear-app-reader-secret", token_service = "linear-app-reader-token", user_id = "00000000-0000-4000-8000-000000000203", renewal = "broker" }\n'
    + f'linear.task_label_group = "{TASK_GROUP}"\n'
    + 'delegates = { default = "harness", harness = { implementation = "ensemble.delegate:HarnessDelegate" } }\n'
    + 'local.issue_prefix = "LOC"\n'
)
STATE_NAMES = dict(
    zip(
        STATES.values(),
        ("Todo", "In Progress", "In Review", "Handoff", "Done", "Canceled", "Backlog"),
        strict=True,
    )
)

ROLES = ("researcher", "pm", "engineer")


PACKAGED_FLOW = files("ensemble.orchestrator.engine.flow").joinpath("workflow.toml").read_text()
_FLOWS = tempfile.mkdtemp(prefix="ensemble-test-workflows-")
atexit.register(shutil.rmtree, _FLOWS, ignore_errors=True)


def workflow_file(text):
    """A workflow file holding text: its path."""
    path = os.path.join(_FLOWS, hashlib.sha256(text.encode()).hexdigest()[:16] + ".toml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def workflow_line(text):
    """A top-level `workflow = "<file>"` line (put it before any table), the file holding text."""
    return f'workflow = "{workflow_file(text)}"\n'


def role_app(name):
    return LinearApp(
        name,
        AppKind.ROLE,
        f"linear.roles.{name}",
        f"client-{name}",
        f"linear-app-{name}-secret",
        f"linear-app-{name}-token",
        role_id(name),
    )


def role_id(name):
    return str(uuid.uuid5(uuid.NAMESPACE_OID, name))


def comment_id(role, sid, *parts):
    app = HARNESS_IDENTITY if role == "harness" else role_app(role)
    return ops.client_id(app.user_id, sid, *parts)


def role(name, *lines):
    """The stage's app table, then its board run settings."""
    app = role_app(name)
    fields = ("client_id", "secret_service", "token_service", "user_id")
    return (
        f"[linear.roles.{name}]\n"
        + "".join(f'{k} = "{getattr(app, k)}"\n' for k in fields)
        + f"[roles.{name}]\n"
        + "".join(f"{x}\n" for x in lines)
    )


def team_node(state_ids=None):
    return {
        "id": TEAM,
        "name": "Team",
        "states": {
            "nodes": [{"id": i, "type": TYPES.get(i, "started")} for i in (state_ids or TYPES)]
        },
    }
