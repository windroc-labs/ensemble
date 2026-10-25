"""<logs_dir>/prune.json: what prune read from Linear and when, kept between ticks."""

import json
import os

from ensemble.orchestrator.engine.flow.types import IssueState, Snapshot
from ensemble.orchestrator.engine.types import Entry, PruneState, Refusal
from ensemble.utils import one_line, parse_time, read_regular
from ensemble.utils import save as save_text

KEYS = {"synced", "issues", "refused", "unbatched"}


def load(path):
    """No file: empty; unreadable: empty, why."""
    raw = read_regular(path)
    if raw is None:
        return PruneState(), ("not a readable file" if os.path.lexists(path) else None)
    try:
        return _state(json.loads(raw)), None
    except (ValueError, TypeError) as e:
        return PruneState(), one_line(e) or type(e).__name__


def save(path, state):
    issues = {
        i: {
            "state": None if e.snapshot is None else e.snapshot.state,
            "since": _iso(None if e.snapshot is None else e.snapshot.since),
            "updated": _iso(None if e.snapshot is None else e.snapshot.updated),
            "gone": e.snapshot is None,
            "read": e.read.isoformat(),
        }
        for i, e in state.issues.items()
    }
    refused = {
        i: {
            "versions": {j: _iso(t) for j, t in r.versions.items()},
            "reason": r.reason,
            "read": r.read.isoformat(),
        }
        for i, r in state.refused.items()
    }
    data = {
        "synced": _iso(state.synced),
        "issues": issues,
        "refused": refused,
        "unbatched": sorted(state.unbatched),
    }
    save_text(path, json.dumps(data, sort_keys=True))


def _state(data):
    if not isinstance(data, dict) or set(data) != KEYS:
        raise ValueError(f"keys are not {sorted(KEYS)}")
    unbatched = data["unbatched"]
    if not isinstance(unbatched, list) or not all(isinstance(i, str) for i in unbatched):
        raise ValueError("unbatched is not a list of idents")
    return PruneState(
        synced=_maybe(data["synced"]),
        issues={i: _entry(e) for i, e in _object(data["issues"]).items()},
        refused={i: _refusal(r) for i, r in _object(data["refused"]).items()},
        unbatched=set(unbatched),
    )


def _entry(data):
    data = _object(data, {"state", "since", "updated", "gone", "read"})
    if not isinstance(data["gone"], bool):
        raise ValueError("gone is not a bool")
    state = None if data["state"] is None else IssueState(data["state"])
    found = (
        None if data["gone"] else Snapshot(state, _maybe(data["since"]), _maybe(data["updated"]))
    )
    return Entry(found, _time(data["read"]))


def _refusal(data):
    data = _object(data, {"versions", "reason", "read"})
    if not isinstance(data["reason"], str):
        raise ValueError("reason is not text")
    versions = {i: _maybe(t) for i, t in _object(data["versions"]).items()}
    return Refusal(versions, data["reason"], _time(data["read"]))


def _object(data, keys=None):
    if not isinstance(data, dict) or (keys is not None and set(data) != keys):
        raise ValueError(f"not an object of {sorted(keys or ())}")
    return data


def _maybe(value):
    return None if value is None else _time(value)


def _time(value):
    if not isinstance(value, str) or (found := parse_time(value)).tzinfo is None:
        raise ValueError(f"not a time with its offset: {str(value)[:40]}")
    return found


def _iso(value):
    return None if value is None else value.isoformat()
