"""The Handoff's reader-side helpers: brief, is_user and build_cutoff."""

import re
from functools import cache

from ensemble.orchestrator.engine.flow import workflow
from ensemble.utils import parse_time


def is_user(note) -> bool:
    return note.origin == "other"


def brief(issue) -> str:
    h = issue.handoff
    return h.instructions if h else issue.description


def _build_started():
    """A build run's start comment: the workflow's build start lead, its colon optional."""
    return _leads_re(
        tuple(
            t.start.removesuffix(":")
            for t in workflow.DEFAULT.values()
            if t.input == "build" and t.start.removesuffix(":")
        )
    )


@cache
def _leads_re(leads):
    # keyed by the workflow's leads: a later load() is seen
    # no lead: no comment is a build's start
    return re.compile(rf"(?:{'|'.join(map(re.escape, leads))})\b" if leads else r"(?!)")


def build_cutoff(issue) -> str:
    started = [
        n.at for n in issue.notes if not is_user(n) and _build_started().match(n.body.strip())
    ]
    return max(started, key=parse_time) if started else issue.created_at
