"""Reads OpenCode's opencode.db read-only: a session's last activity, the newest session of a
title, and a session's user and assistant texts as JSON lines,
`python3 -I transcript.py SES_ID [--db PATH] [--limit N]`, newest N oldest first. Standalone: the
standard library only.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from urllib.parse import quote

NATIVE = re.compile(r"ses_[0-9A-Za-z]{1,128}")
TIMEOUT = 1  # seconds a locked db is waited for
LIMIT, LIMIT_MAX = 100, 10_000
ENTRIES = (
    "SELECT m.time_created, m.data, p.data FROM part p JOIN message m ON m.id = p.message_id"
    " WHERE p.session_id = ?"
    " ORDER BY m.time_created DESC, m.id DESC, p.time_created DESC, p.id DESC"
)


class Refused(Exception):
    pass


def database(env: Mapping[str, str]) -> str:
    """OpenCode's db under XDG_DATA_HOME when absolute, else ~/.local/share."""
    data = env.get("XDG_DATA_HOME", "")
    if not os.path.isabs(data):
        data = os.path.expanduser("~/.local/share")
    return os.path.join(data, "opencode", "opencode.db")


def connect(path: str) -> sqlite3.Connection:
    if not os.path.isfile(path):
        raise Refused(f"no database {path}")
    try:
        return sqlite3.connect(f"file:{quote(path)}?mode=ro", uri=True, timeout=TIMEOUT)
    except sqlite3.Error as error:
        raise Refused(f"{path}: {error}") from None


def updated(path: str, session: str) -> float | None:
    """The session's time_updated in seconds; None when it has no row."""
    db = connect(path)
    try:
        row = db.execute("SELECT time_updated FROM session WHERE id = ?", (session,)).fetchone()
    except sqlite3.Error as error:
        raise Refused(f"{path}: {error}") from None
    finally:
        db.close()
    if row is None:
        return None
    value = row[0]
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise Refused(f"{path}: session {session}: time_updated is no number")
    return value / 1000


def titled(path: str, title: str) -> str | None:
    """The newest top-level session titled `title`; None when it has none (or a malformed id)."""
    db = connect(path)
    try:
        row = db.execute(
            "SELECT id FROM session WHERE title = ? AND parent_id IS NULL"
            " ORDER BY time_created DESC, id DESC LIMIT 1",
            (title,),
        ).fetchone()
    except sqlite3.Error as error:
        raise Refused(f"{path}: {error}") from None
    finally:
        db.close()
    found = row[0] if row is not None else None
    return found if isinstance(found, str) and NATIVE.fullmatch(found) else None


def _stamp(ms) -> str:
    when = datetime.fromtimestamp(ms / 1000, UTC)
    return when.isoformat(timespec="milliseconds").removesuffix("+00:00") + "Z"


def _entry(created, message: str, part: str) -> dict | None:
    try:
        role = json.loads(message).get("role")
        data = json.loads(part)
        if role not in ("user", "assistant") or data.get("type") != "text":
            return None
        value = data.get("text")
        if not isinstance(value, str) or not value.strip() or data.get("synthetic"):
            return None
        if data.get("ignored"):
            return None
        return {"time": _stamp(created), "role": role, "text": value}
    except (ValueError, TypeError, AttributeError, OverflowError, OSError):
        return None


def entries(path: str, session: str, limit: int) -> list[dict]:
    """The session's newest `limit` text parts of user and assistant messages, oldest first."""
    db = connect(path)
    try:
        if db.execute("SELECT 1 FROM session WHERE id = ?", (session,)).fetchone() is None:
            raise Refused(f"no session {session}")
        found: list[dict] = []
        for row in db.execute(ENTRIES, (session,)):
            if (entry := _entry(*row)) is not None:
                found.append(entry)
                if len(found) == limit:
                    break
    except sqlite3.Error as error:
        raise Refused(f"{path}: {error}") from None
    finally:
        db.close()
    return found[::-1]


def _limit(value: str) -> int:
    if not value.isdigit() or not 1 <= int(value) <= LIMIT_MAX:
        raise argparse.ArgumentTypeError(f"want 1-{LIMIT_MAX}")
    return int(value)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="transcript.py",
        description="An OpenCode session's user and assistant texts as JSON lines.",
    )
    parser.add_argument("session", help="the session id, ses_…")
    parser.add_argument("--db", default=database(os.environ), help="default: %(default)s")
    parser.add_argument("--limit", type=_limit, default=LIMIT, help="default: %(default)s")
    args = parser.parse_args(argv)
    if not NATIVE.fullmatch(args.session):
        parser.error(f"not an OpenCode session id: {args.session!r:.80}")
    try:
        found = entries(os.path.abspath(args.db), args.session, args.limit)
    except Refused as error:
        print(f"opencode transcript: {error}", file=sys.stderr)
        return 1
    for entry in found:
        print(json.dumps(entry, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
