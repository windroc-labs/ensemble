"""Run records: <workdir>/run.jsonl, one JSON object {"ts", "kind", **fields} per line, through
utils.files; a read returns what it can. The agent run can append to the file: what a read finds is
a status hint, never an authorization or a recovery grant.
"""

import json
import os
import sys
from datetime import datetime

from ensemble.session.engine.types import SessionInfo
from ensemble.utils import append_line, json_records, read_regular

CHANNEL = "run.jsonl"


class RunLog:
    def __init__(self, workdir: str):
        self.path = os.path.join(os.path.abspath(workdir), CHANNEL)

    @staticmethod
    def stamp() -> str:
        return datetime.now().astimezone().isoformat(timespec="milliseconds")

    def note(
        self, kind: str, *, best_effort: bool = False, durable: bool = False, **fields
    ) -> None:
        try:
            append_line(
                self.path,
                json.dumps({"ts": self.stamp(), "kind": kind, **fields}, ensure_ascii=False) + "\n",
                durable=durable,
            )
        except OSError as e:
            if not best_effort:
                raise
            print(f"records: {e}", file=sys.stderr)

    def read(self) -> list[dict]:
        data = read_regular(self.path)
        if data is None:
            return []
        return list(json_records(data.split(b"\n")))

    def session(self, sid: str) -> SessionInfo | None:
        """The latest valid `session` line of `sid`, with its later valid `session_metadata` lines
        folded in. A forged valid-looking line is indistinguishable: callers re-check what they act
        on (delegate's plan.place re-checks cwd against trusted_dirs).
        """
        found = None
        for e in self.read():
            if e.get("sid") != sid:
                continue
            kind = e.get("kind")
            if kind == "session":
                found = SessionInfo.from_fields(e) or found
            elif found is None:
                continue
            elif kind == "session_metadata":
                fields = {
                    **found.persisted(),
                    **{k: e[k] for k in ("transcript", "resume") if k in e},
                }
                found = SessionInfo.from_fields(fields) or found
        return found
