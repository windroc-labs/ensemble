"""Orchestrator event logging, rotated to orchestrator.jsonl.1 at LOG_MAX, and bounded outage
reporting."""

import contextlib
import json
import os
import sys
from datetime import UTC, datetime, timedelta

from ensemble.orchestrator.engine.types import ConfigError, LinearError
from ensemble.utils import append_line, file_lock, one_line, printable, timestamp

LOGS_DIR = None
CAPTURED = None
RECENT: dict[str, dict[str, datetime]] | None = None


def use_logs(path):
    global LOGS_DIR
    LOGS_DIR = path


@contextlib.contextmanager
def captured():
    """Each line logged inside, a dedupe-skipped one too, as (line, shown, said): whether stderr
    showed it, and the logger's own stderr text for it (else None). Nested captures share
    once-history timestamps; the outer invocation loads each path once and releases them on exit.
    """
    global CAPTURED, RECENT
    outer, recent = CAPTURED, RECENT
    CAPTURED = []
    if RECENT is None:
        RECENT = {}
    try:
        yield CAPTURED
    finally:
        CAPTURED, RECENT = outer, recent


ONCE = ("src", "kind", "issue", "entry", "reason")
WINDOW = timedelta(hours=24)
LOG_MAX = 10 << 20


def log(
    src,
    kind,
    issue=None,
    *,
    once=False,
    dry=False,
    quiet=False,
    told=False,
    now=datetime.now,
    **fields,
):
    """told: the caller says it on stderr itself."""
    _log(
        _line(src, kind, issue, now=now, **fields),
        LOGS_DIR,
        once=once,
        dry=dry,
        quiet=quiet,
        told=told,
        now=now,
    )


def _line(src, kind, issue=None, *, now=datetime.now, **fields):
    return {
        k: v
        for k, v in {
            "ts": timestamp(now=now),
            "src": src,
            "kind": kind,
            "issue": issue,
            **fields,
        }.items()
        if v is not None
    }


def _log(line, logs, *, once, dry, quiet=False, told=False, said=None, now=datetime.now):
    path, key = os.path.join(logs, "orchestrator.jsonl") if logs else None, _key(line)
    at = datetime.fromisoformat(line["ts"])
    if once and not dry and path:
        recent = _recent_keys(path, now=now)
        if key in recent and recent[key] >= now(UTC) - WINDOW:
            _capture(line, told, said)
            return
    text = json.dumps(line, ensure_ascii=False)
    try:
        if not dry and path and logs:
            os.makedirs(logs, exist_ok=True)
            _rotate(path)
            append_line(path, text + "\n")
            if RECENT is not None and path in RECENT:
                RECENT[path][key] = max(at, RECENT[path].get(key, at))
    except OSError:
        pass
    shown = told
    try:
        if dry or not quiet and sys.stderr.isatty():
            print(printable(text), file=sys.stderr, flush=True)
            shown = True
    except OSError:  # a gone tmux pane's EIO
        pass
    _capture(line, shown, said)


def _capture(line, shown, said):
    if CAPTURED is not None:
        CAPTURED.append((line, shown, said))


def _rotate(path):
    """path -> path.1 at LOG_MAX, rechecked under its lock so another process's rotation is not
    repeated; appends stay unlocked (launchd writes the file too); a failure still lets them append.
    """
    try:
        if _size(path) >= LOG_MAX:
            with file_lock(path):
                if _size(path) >= LOG_MAX:
                    os.replace(path, path + ".1")
    except OSError:
        pass


def _size(path):
    try:
        return os.stat(path).st_size
    except FileNotFoundError:
        return 0


def config_error(src, e, *, dry=False, now=datetime.now):
    reason = one_line(str(e) if isinstance(e, (LinearError, ConfigError)) else e)
    logs = e.logs_dir if isinstance(e, ConfigError) else LOGS_DIR
    _stop(src, "config-error", reason, dry, logs=logs, reason=reason, now=now)
    return 2


def token_error(src, *, dry=False):
    """A client.use report: a failed renewal of a still usable app token, as a token-error line."""
    return lambda identity, reason: log(
        src, "token-error", dry=dry, identity=identity, reason=reason
    )


def token_state(src, identity, fields):
    """A dry run's report of an app's token: days_left, or a state (missing, no expiry, expired)."""
    log(src, "token", dry=True, identity=identity, **fields)


def linear_error(src, e, *, dry=False, now=datetime.now):
    return _stop(
        src,
        "linear-error",
        f"Linear unavailable: {e}",
        dry,
        logs=LOGS_DIR,
        now=now,
        op=e.op,
        status=e.status,
        reason=e.reason,
    )


def _stop(src, kind, message, dry, *, logs, now=datetime.now, **fields):
    said, tty = f"{src}.py: {message}", sys.stderr.isatty() and not dry
    _log(
        _line(src, kind, now=now, **fields),
        logs,
        once=True,
        dry=dry,
        told=tty,
        said=said,
        now=now,
    )
    try:
        if tty:  # under a scheduler stderr is the log
            print(said, file=sys.stderr, flush=True)
    except OSError:
        pass
    return 1


def _key(line):
    return json.dumps([line.get(k) for k in ONCE])


def _recent_keys(path, *, now=datetime.now):
    if RECENT is not None and path in RECENT:
        return RECENT[path]
    cutoff, keys = now(UTC) - WINDOW, {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for raw in f:
                try:
                    line = json.loads(raw)
                    at = datetime.fromisoformat(line["ts"])
                    if at >= cutoff:
                        key = _key(line)
                        keys[key] = max(at, keys.get(key, at))
                except (ValueError, TypeError, KeyError):
                    pass
    except OSError:
        pass
    if RECENT is not None:
        RECENT[path] = keys
    return keys
