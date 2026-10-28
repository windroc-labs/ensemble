"""A temp local board: MTIME, issue file text, the Board case that writes files."""

import os
from datetime import UTC, datetime

from tests.orchestrator.case import Case, zone

MTIME = datetime(2026, 10, 20, tzinfo=UTC)


def text(ident, *, front="", description="", comments="", history=""):
    body = f"### Description\n{description}\n" if description else ""
    body += f"### Comments\n{comments}" if comments else ""
    body += f"### History\n{history}" if history else ""
    return f"---\nid: {ident}\ntitle: {ident} title\ncreated: 2026-10-19T14:02:00-04:00\n{front}---\n{body}"


class Board(Case):
    def setUp(self):
        zone(self)
        self.board = os.path.join(os.path.realpath(self.home), "board")
        os.mkdir(self.board)

    def write(self, rel, ident, *, mtime=MTIME, **parts):
        path = os.path.join(self.board, rel, f"{ident} x.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as file:
            file.write(text(ident, **parts))
        os.utime(path, (mtime.timestamp(), mtime.timestamp()))
        return path
