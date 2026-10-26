"""The runs log (runs.jsonl): one durable JSON line per run event; its readers: start and resume
lines as Entries, a sid's start row as its Claim and its task (`started_task`), live limit rows
(`limits`), `failed` rows (a new run that failed before its LinearRun) as Failures, `busy` rows (a
run whose lock was held; one may cancel a start or resume row) as Busy, `trim` to KEEP.
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


class Busy(NamedTuple):
    ts: datetime
    issue: str
    sid: str
    cancels: tuple[str, datetime] | None  # the cancelled row's kind and ts
    attempt: int | None  # the busy Finished's; None: the router's


def _row(kind, now, fields):
    return json.dumps({"ts": timestamp(now=now), "kind": kind, **fields}, ensure_ascii=False) + "\n"


def note(path, kind, *, now=datetime.now, **fields) -> str:
    """Returns the row's ts."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = _row(kind, now, fields)
    append_line(path, line, durable=True, locked=True)
    return json.loads(line)["ts"]


def note_busy(path, *, issue, sid, cancels: tuple[str, str] | None, now=datetime.now):
    """The router's busy row; cancels: the (kind, ts) of the row it cancels."""
    shown = cancels and {"kind": cancels[0], "ts": cancels[1]}
    note(path, "busy", issue=issue, sid=sid, cancels=shown, attempt=None, now=now)


def note_busy_finished(path, *, issue, sid, attempt) -> str | None:
    """A busy Finished's row, cancelling the (issue, sid) latest start or resume row unless a busy
    row cancels it or has this attempt already; the kind of the row this attempt's busy row cancels.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with file_lock(path):
        lines = _lines(path)
        found = [b for line in lines if (b := _busy(line)) and (b.issue, b.sid) == (issue, sid)]
        if mine := next((b for b in found if b.attempt == attempt), None):
            return mine.cancels and mine.cancels[0]
        rows = [(e, line) for line in lines if (e := parse_line(line)) and e.sid == sid]
        rows = [(e, line) for e, line in rows if e.issue == issue]
        if not rows:
            return None
        newest, line = max(enumerate(rows), key=lambda r: (r[1][0].ts, r[0]))[1]
        if any(cancelled(b, newest) for b in found):
            return None
        fields = {
            "issue": issue,
            "sid": sid,
            "cancels": {"kind": newest.kind, "ts": json.loads(line)["ts"]},
            "attempt": attempt,
        }
        append_line(path, _row("busy", datetime.now, fields), durable=True)
        return newest.kind


def note_limit(path, *, issue, sid, client, resets_at, delegate_kind, attempt):
    """Once per (issue, sid, attempt): a redelivery after a lost ledger write appends none."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fields = {
        "issue": issue,
        "sid": sid,
        "client": client,
        "resets_at": resets_at,
        "delegate_kind": delegate_kind,
        "attempt": attempt,
    }
    key = ("limit", issue, sid, attempt)
    with file_lock(path):
        if not any(_limit_key(line) == key for line in _lines(path)):
            append_line(path, _row("limit", datetime.now, fields), durable=True)


def _limit_key(line):
    try:
        d = json.loads(line)
    except ValueError:
        return None
    return isinstance(d, dict) and tuple(d.get(k) for k in ("kind", "issue", "sid", "attempt"))


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
    "key": lambda v: v is None or _text(v),
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
                d["key"],
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


def _limit(line, now):
    try:
        d = json.loads(line)
        if (
            d["kind"] == "limit"
            and _client(d)
            and _text(d["delegate_kind"])
            and isinstance(d["resets_at"], str)
        ):
            at = datetime.fromisoformat(d["resets_at"])
            if at.utcoffset() is not None and now < at <= now + KEEP:
                return (d["delegate_kind"], d["client"]), at
    except ERRORS:
        pass
    return None


def _failure(line):
    try:
        d = json.loads(line)
        if d["kind"] == "failed" and isinstance(d["issue"], str):
            return Failure(datetime.fromisoformat(d["ts"]).astimezone(UTC), d["issue"])
    except ERRORS:
        pass
    return None


def _busy(line):
    try:
        d = json.loads(line)
        cancels, attempt = d["cancels"], d["attempt"]
        if (
            d["kind"] == "busy"
            and isinstance(d["issue"], str)
            and isinstance(d["sid"], str)
            and (attempt is None or type(attempt) is int and attempt >= 0)
        ):
            if cancels is not None:
                if cancels["kind"] not in ("start", "resume"):
                    return None
                cancels = cancels["kind"], datetime.fromisoformat(cancels["ts"]).astimezone(UTC)
            ts = datetime.fromisoformat(d["ts"]).astimezone(UTC)
            return Busy(ts, d["issue"], d["sid"], cancels, attempt)
    except ERRORS:
        pass
    return None


def busy_rows(path):
    return [b for line in _lines(path) if (b := _busy(line))]


def cancelled(busy: Busy, entry: Entry) -> bool:
    return (busy.issue, busy.sid, busy.cancels) == (entry.issue, entry.sid, (entry.kind, entry.ts))


def cancelled_rows(busy, rows) -> int:
    """How many of rows (Entries; other rows never count) a busy row cancels."""
    return sum(1 for e in rows if isinstance(e, Entry) and any(cancelled(b, e) for b in busy))


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


def limits(path, now):
    found = {}
    for key, at in filter(None, (_limit(line, now) for line in _lines(path))):
        if key not in found or at > found[key]:
            found[key] = at
    return found


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
        kept = {e for e in entries if e.ts >= now - KEEP or e.sid == newest[e.issue].sid}
        keep = [
            line
            for line in lines
            if parse_line(line) in kept
            or _limit(line, now)
            or (f := _failure(line))
            and f.ts >= now - KEEP
            or (b := _busy(line))
            and (any(cancelled(b, e) for e in kept) if b.cancels else b.ts >= now - KEEP)
        ]
        if len(keep) < len(lines):
            save(path, "".join(keep))


def client_of(entries, sid):
    mine = [e for e in entries if e.sid == sid]
    return next((e for e in mine if e.kind == "start"), min(mine, key=lambda e: e.ts)).client


def delegate_of(entries, sid) -> str:
    """Raises ConfigError when a row of the sid has none."""
    mine = [e for e in entries if e.sid == sid]
    first = next((e for e in mine if e.kind == "start"), min(mine, key=lambda e: e.ts))
    if any(e.delegate_kind is None for e in mine):
        raise ConfigError(f"{first.issue}: session {sid}'s runs.jsonl row has no delegate kind")
    return first.delegate_kind
