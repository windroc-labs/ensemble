"""The runs log (runs.jsonl): one durable JSON line per run event; its readers: start and resume
lines as Entries, a sid's start row as its Claim and its task (`started_task`), `failed` rows
(a new run that failed before its LinearRun) as Failures, `trim` to KEEP.
"""

import json
import os
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from ensemble.orchestrator.engine.types import Claim, ConfigError
from ensemble.utils import append_line, file_lock, repo_slug, save, timestamp

KEEP = timedelta(days=7)
ERRORS = (ValueError, TypeError, KeyError, OverflowError)


class Entry(NamedTuple):
    ts: datetime
    kind: str
    issue: str
    sid: str
    role: str
    client: str
    delegate_kind: str | None


class Failure(NamedTuple):
    ts: datetime
    issue: str


def _row(kind, now, fields):
    return json.dumps({"ts": timestamp(now=now), "kind": kind, **fields}, ensure_ascii=False) + "\n"


def note(path, kind, *, now=datetime.now, **fields):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    append_line(path, _row(kind, now, fields), durable=True, locked=True)


def _client(d):
    return isinstance(d["client"], str) and d["client"] != ""


def _text(value):
    return isinstance(value, str) and value != ""


def parse_line(line):
    try:
        d = json.loads(line)
        if (
            d["kind"] in ("start", "resume")
            and all(isinstance(d[k], str) for k in ("issue", "sid", "role"))
            and _client(d)
            and ("delegate_kind" not in d or _text(d["delegate_kind"]))
        ):
            ts = datetime.fromisoformat(d["ts"]).astimezone(UTC)
            return Entry(
                ts,
                d["kind"],
                d["issue"],
                d["sid"],
                d["role"],
                d["client"],
                d.get("delegate_kind"),
            )
    except ERRORS:
        pass
    return None


CLAIMED = {
    "role": _text,
    "uuid": _text,
    "project": lambda v: v is None or isinstance(v, str),
    "target": lambda v: v is None or repo_slug(v) is not None,
    "client": _text,
    "delegate_kind": _text,
}


def claim(path, issue, sid) -> Claim:
    """The (issue, sid) start row's Claim, never another sid's; raises ValueError."""
    for line in _lines(path):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict) and (d.get("kind"), d.get("issue"), d.get("sid")) == (
            "start",
            issue,
            sid,
        ):
            for name, valid in CLAIMED.items():
                if name not in d or not valid(d[name]):
                    raise ValueError(f"{issue}: session {sid}'s start row {name}: {d.get(name)!r}")
            return Claim(
                issue,
                sid,
                d["role"],
                d["uuid"],
                d["project"],
                d["target"] and repo_slug(d["target"]),
                d["client"],
                d["delegate_kind"],
            )
    raise ValueError(f"{issue}: no start row for session {sid}")


def started_task(path, issue, sid) -> str | None:
    """The (issue, sid) start row's task; None when it has none (no label, or a row written before
    start rows recorded it) or there is no such row.
    """
    for line in _lines(path):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict) and (d.get("kind"), d.get("issue"), d.get("sid")) == (
            "start",
            issue,
            sid,
        ):
            return d["task"] if _text(d.get("task")) else None
    return None


def _failure(line):
    try:
        d = json.loads(line)
        if d["kind"] == "failed" and isinstance(d["issue"], str):
            return Failure(datetime.fromisoformat(d["ts"]).astimezone(UTC), d["issue"])
    except ERRORS:
        pass
    return None


def failures(path):
    return [f for line in _lines(path) if (f := _failure(line))]


def _lines(path):
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return f.readlines()
    except FileNotFoundError:
        return []


def parse_log(path):
    return [e for line in _lines(path) if (e := parse_line(line))]


def latest(entries):
    found = {}
    for entry in entries:
        if entry.issue not in found or entry.ts >= found[entry.issue].ts:
            found[entry.issue] = entry
    return found


def trim(path, now):
    with file_lock(path):
        lines = _lines(path)
        entries = [e for line in lines if (e := parse_line(line))]
        newest = latest(entries)
        keep = [
            line
            for line in lines
            if (e := parse_line(line))
            and (e.ts >= now - KEEP or e.sid == newest[e.issue].sid)
            or (f := _failure(line))
            and f.ts >= now - KEEP
        ]
        if len(keep) < len(lines):
            save(path, "".join(keep))


def client_of(entries, sid):
    mine = [e for e in entries if e.sid == sid]
    return next((e for e in mine if e.kind == "start"), min(mine, key=lambda e: e.ts)).client


def role_of(entries, sid):
    """A session's recorded role, independent of the board's current assignment."""
    mine = [e for e in entries if e.sid == sid]
    return next((e for e in mine if e.kind == "start"), min(mine, key=lambda e: e.ts)).role


def delegate_of(entries, sid) -> str:
    """Raises ConfigError when a row of the sid has none."""
    mine = [e for e in entries if e.sid == sid]
    first = next((e for e in mine if e.kind == "start"), min(mine, key=lambda e: e.ts))
    if any(e.delegate_kind is None for e in mine):
        raise ConfigError(f"{first.issue}: session {sid}'s runs.jsonl row has no delegate kind")
    return first.delegate_kind
