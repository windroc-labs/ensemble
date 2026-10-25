"""Orchestrator event logging, rotated to orchestrator.jsonl.1 at LOG_MAX, and bounded outage
reporting."""

import json
import os
import sys
from datetime import UTC, datetime, timedelta

from ensemble.orchestrator.engine.types import ConfigError, LinearError
from ensemble.utils import append_line, file_lock, one_line, printable, timestamp

LOGS_DIR = None


def use_logs(path):
    global LOGS_DIR
    LOGS_DIR = path


ONCE = ("src", "kind", "issue", "entry", "reason")
WINDOW = timedelta(hours=24)
LOG_MAX = 10 << 20


def log(src, kind, issue=None, *, once=False, dry=False, quiet=False, now=datetime.now, **fields):
    _log(
        _line(src, kind, issue, now=now, **fields),
        LOGS_DIR,
        once=once,
        dry=dry,
        quiet=quiet,
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


def _log(line, logs, *, once, dry, quiet=False, now=datetime.now):
    path, key = os.path.join(logs, "orchestrator.jsonl") if logs else None, _key(line)
    if once and not dry and path and key in _recent_keys(path, now=now):
        return
    text = json.dumps(line, ensure_ascii=False)
    try:
        if not dry and path and logs:
            os.makedirs(logs, exist_ok=True)
            _rotate(path)
            append_line(path, text + "\n")
    except OSError:
        pass
    try:
        if dry or not quiet and sys.stderr.isatty():
            print(printable(text), file=sys.stderr, flush=True)
    except OSError:  # a gone tmux pane's EIO
        pass


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
    _log(_line(src, kind, now=now, **fields), logs, once=True, dry=dry, now=now)
    try:
        if sys.stderr.isatty() and not dry:  # under a scheduler stderr is the log
            print(f"{src}.py: {message}", file=sys.stderr, flush=True)
    except OSError:
        pass
    return 1


def _key(line):
    return json.dumps([line.get(k) for k in ONCE])


def _recent_keys(path, *, now=datetime.now):
    cutoff, keys = now(UTC) - WINDOW, set()
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for raw in f:
                try:
                    line = json.loads(raw)
                    if datetime.fromisoformat(line["ts"]) >= cutoff:
                        keys.add(_key(line))
                except (ValueError, TypeError, KeyError):
                    pass
    except OSError:
        pass
    return keys
