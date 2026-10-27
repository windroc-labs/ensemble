"""A guarded Docker worker's activity word: written by its host hook, read by its handle, rows and
views.
"""

import os

from ensemble.session.engine.status_log import SessionStatusLog
from ensemble.session.engine.types import StatusState
from ensemble.utils import read_regular, save

MARKS = (StatusState.WORKING, StatusState.DONE, StatusState.BLOCKED)


def activity_file(state: str, generation: str) -> str:
    return os.path.join(state, f"{generation}.activity")


def activity(state: str, generation: str) -> StatusState | None:
    try:
        word = (read_regular(activity_file(state, generation)) or b"").decode()
    except (OSError, UnicodeError):
        return None
    return StatusState(word) if word in MARKS else None


def write_activity(state: str, generation: str, word: str, events: str | None, name: str) -> None:
    """Its activity file, then for done and blocked the events line."""
    save(activity_file(state, generation), word)
    if word in (StatusState.DONE, StatusState.BLOCKED) and events:
        SessionStatusLog(events).write(name, word)
