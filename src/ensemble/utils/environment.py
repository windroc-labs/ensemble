"""Environment names and values passed to child processes."""

import os
from collections.abc import Mapping

SID_ENV = "ENSEMBLE_SID"
ACCOUNT_ENV = "ENSEMBLE_ACCOUNT"
EVENTS_ENV = "ENSEMBLE_EVENTS"

LEASE_ENV = (
    "ENSEMBLE_MANAGER",
    "ENSEMBLE_HOLDER_ID",
    "ENSEMBLE_HOLDER_GENERATION",
    "ENSEMBLE_HOLDER_PID",
)

# on every agent client a session starts: nothing under it starts a session or run
AGENT_SESSION_ENV = "ENSEMBLE_AGENT_SESSION"
# and the name of the client it runs, when the session has one
CLIENT_ENV = "ENSEMBLE_CLIENT"


def nested_start() -> str | None:
    """None outside an agent session ensemble started; inside one, why a start refuses."""
    if not os.environ.get(AGENT_SESSION_ENV):
        return None
    return (
        f"refused inside an agent session ensemble started (${AGENT_SESSION_ENV} is set): "
        "only a client you start starts sessions and runs"
    )


def environment_ok(value: object) -> bool:
    """Accept exec names: nonempty, no '=' or NUL; string values without NUL.

    Names need not be shell identifiers. Render them as env operands in shell commands.
    """
    return isinstance(value, Mapping) and all(
        isinstance(name, str)
        and bool(name)
        and "=" not in name
        and "\0" not in name
        and isinstance(item, str)
        and "\0" not in item
        for name, item in value.items()
    )
