"""Run-side fixtures: ids, the config files config.load() reads (in the test's HOME), a Linear fake
recording each call's identity, and the orchestrator log's readers.
"""

import json
import os
import shutil
import tempfile
from datetime import datetime
from unittest import mock

from ensemble.orchestrator.engine import (
    log,
)
from ensemble.orchestrator.engine.types import LinearError
from ensemble.orchestrator.trackers.linear import (
    linear_tracker,
    ops,
)
from tests.orchestrator.case import Case
from tests.orchestrator.unit.board_ids import HEADER, STATE_NAMES, STATES, role
from tests.utils.case import local_section

ID, UUID = "TASK-7", "11111111-2222-4333-8444-555555555555"

SID = "0b6f2c1e-6d0a-4c1b-9a51-3f1f6b0e2a7d"

PROJECT = "121166b1-191a-4461-bec4-42f1c2dc0ddd"

ENGINEER, RESEARCHER = "engineer@agents.test", "researcher@agents.test"

HARNESS, KEY = "linear-api-key", "linear-api-key-engineer"

SESSION = ops.client_id(SID, "session")

NOT_FOUND = LinearError("linear api error: [{'message': 'Entity not found: Comment'}]")

URL = f"https://linear.app/t/issue/{ID}"

USER_NOTE = {
    "body": "Use SQLite.",
    "createdAt": "2026-09-02T00:00:00.000Z",
    "user": {"email": "ME@x.com", "name": "Me", "isMe": False},
}

CORE_LOCAL = """[delegate.roles.researcher.output]
type = "github"
repo = "windroc/team-docs"
branch = "main"
dir = "Research/"

[delegate.roles.pm.output]
type = "github"
repo = "windroc/team-docs"
branch = "main"
dir = "Product Design/"
"""


def local(work):
    return (
        HEADER
        + 'linear.human_members = ["Me@X.com"]\n'
        + f'work_dir = "{work}"\n'
        + role("researcher")
        + role("pm")
        + role("engineer")
        + f'[linear.project_repos]\n"{PROJECT}" = "acme/ensemble"\n'
    )


def logged(logs=None):
    try:
        with open(os.path.join(logs or log.LOGS_DIR, "orchestrator.jsonl"), encoding="utf-8") as f:
            lines = [json.loads(line) for line in f]
    except FileNotFoundError:
        return []
    for d in lines:
        assert (
            list(d)[:3] == ["ts", "src", "kind"]
            and datetime.fromisoformat(d["ts"]).utcoffset() is not None
        ), d
    return lines


def show(event):
    fields = [
        f"{k}={v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)}"
        for k, v in event.items()
        if k not in ("ts", "src", "kind", "issue", "traceback")
    ]
    return " ".join([event["kind"], *([event["issue"]] if "issue" in event else []), *fields])


def node(title="ENG: Session registry", description="Add a session registry.", comments=()):
    return {
        "id": UUID,
        "identifier": ID,
        "url": URL,
        "title": title,
        "description": description,
        "createdAt": "2026-09-01T00:00:00.000Z",
        "project": {"id": PROJECT},
        "comments": {"nodes": list(comments)},
        "attachments": {"nodes": []},
        "relations": {"nodes": []},
        "inverseRelations": {"nodes": []},
        "labels": {"nodes": []},
    }


NAMES = {
    linear_tracker.Q_READ: "issue",
    ops.M_COMMENT: "post",
    linear_tracker.M_UPDATE: "update",
    ops.M_COMMENT_ID: "comment",
    ops.Q_COMMENT: "find",
    linear_tracker.Q_ATTACHED: "read",
    ops.Q_ISSUE_STATE: "reread",
    ops.M_STATE: "state",
    ops.M_SUBSCRIBE: "subscribe",
    linear_tracker.M_ATTACH: "attach",
    linear_tracker.M_TITLE: "title",
    linear_tracker.Q_SEARCH: "search",
    linear_tracker.Q_SEARCH_IDS: "search",
    linear_tracker.Q_IDS: "search",
    linear_tracker.Q_UPSTREAM: "upstream",
}

FIELDS = {
    "update": "commentUpdate",
    "comment": "commentCreate",
    "post": "commentCreate",
    "state": "issueUpdate",
    "title": "issueUpdate",
    "subscribe": "issueSubscribe",
    "attach": "attachmentLinkURL",
}


class Gql:
    """Linear: calls recorded as (name, service, vars); comments kept by client id, the issue as its
    UUID; `issue`: the issue read's node; `fail(name, vars)` returns an exception to raise.
    """

    def __init__(self, fail=lambda _name, _v: None, issue=None):
        self.fail, self.issue, self.calls, self.timeouts, self.comments = fail, issue, [], [], {}
        self.found = []

    def __call__(self, query, *, service, timeout=30, **v):
        if query == linear_tracker.Q_STATE_NAME:
            return {"workflowState": {"name": STATE_NAMES[v["i"]]}}
        name = NAMES[query]
        self.calls.append((name, service, v))
        self.timeouts.append(timeout)
        if (e := self.fail(name, v)) is not None:
            raise e
        if name == "issue":
            return {"issue": self.issue}
        if name == "search":
            return {"searchIssues": {"nodes": list(self.found)}, "issues": {"nodes": []}}
        if name == "upstream":
            return {"issue": None}
        if name == "find":
            if v["c"] not in self.comments:
                raise NOT_FOUND
            body, issue = self.comments[v["c"]]
            return {
                "comment": {
                    "id": v["c"],
                    "body": body,
                    "issue": {"id": issue},
                    "user": {"isMe": True},
                }
            }
        if name == "read":
            return {"issue": {"attachments": {"nodes": []}}}
        if name == "reread":
            return {"issue": {"state": {"id": STATES["in_progress"]}}}
        if name == "update":
            if v["c"] not in self.comments:
                raise NOT_FOUND
            self.comments[v["c"]] = (v["b"], self.comments[v["c"]][1])
        elif name == "comment":
            self.comments[v["c"]] = (v["b"], UUID if v["i"] == ID else v["i"])
        return {FIELDS[name]: {"success": True}}


class Base(Case):
    def setUp(self):
        self.work = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)
        self.local = os.path.join(self.ensemble, "config.local.toml")
        self.write_local(local(self.work))
        self.rd = os.path.join(self.work, "work", ID)
        self.logs = os.path.join(self.work, "logs")
        self.runs = os.path.join(self.logs, "runs.jsonl")
        log.use_logs(None)
        for p in (mock.patch.dict(os.environ),):
            p.start()
            self.addCleanup(p.stop)
        self.gql = Gql()

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def write_local(self, text):
        """~/.ensemble/config.local.toml: `text`, then CORE_LOCAL (the delegate's keys)."""
        self.write(self.local, local_section(text, "orchestrator") + "\n" + CORE_LOCAL)

    def said(self, src=None):
        return [
            show(e) if src else f"{e['src']} {show(e)}"
            for e in logged(self.logs)
            if src in (None, e["src"])
        ]
