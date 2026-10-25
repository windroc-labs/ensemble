import atexit
import hashlib
import os
import shutil
import tempfile
from importlib.resources import files

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
# The Linear tracker, then dotted keys and inline tables: fixtures may append top-level keys (a
# Linear one as linear.<key>) or tables ([linear.<table>] included) after it.
HEADER = (
    'tracker = "linear"\n'
    + 'linear.team = "{}"\nlinear.harness_key = "linear-api-key"\nlinear.states = {{ {} }}\n'.format(
        TEAM, ", ".join(f'{k} = "{v}"' for k, v in STATES.items())
    )
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

ACCOUNTS = {r: f"{r}@agents.test" for r in ("researcher", "pm", "engineer")}


# the packaged workflow's text, for a fixture that changes it
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


def role(name, *lines):
    """[linear.roles.<name>]'s account and key, then [roles.<name>] holding `lines`."""
    return (
        f'[linear.roles.{name}]\naccount = "{name}@agents.test"\nkey = "linear-api-key-{name}"\n'
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
