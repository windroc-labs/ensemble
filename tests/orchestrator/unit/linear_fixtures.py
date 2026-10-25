"""Linear boundary fixtures: GraphQL responses, logs and board data."""

import io
import json
import os
import sys
import tempfile
import urllib.request
from datetime import datetime
from unittest import mock

from ensemble.orchestrator.engine import log
from ensemble.orchestrator.engine.types import LinearError
from ensemble.orchestrator.trackers.linear import client, ops
from tests.orchestrator.case import Case
from tests.orchestrator.unit.board_ids import STATES
from tests.orchestrator.unit.streams import Stderr

LABEL1, LABEL2 = "00000000-0000-4000-8000-000000000021", "00000000-0000-4000-8000-000000000022"

OTHER = "00000000-0000-4000-8000-000000000023"

SECRET = "lin_api_SECRET"

VIEWER = "query { viewer { id } }"


def respond(body=b'{"data": {}}'):
    resp = mock.MagicMock()
    resp.__enter__.return_value = io.BytesIO(body)
    return resp


def refused(case, call):
    with (
        mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("request sent")),
        mock.patch.object(
            urllib.request, "build_opener", side_effect=AssertionError("request sent")
        ),
        case.assertRaises(LinearError) as cm,
    ):
        call()
    case.assertIsInstance(str(cm.exception), str)
    return str(cm.exception)


def http_error(code, body=b""):
    return urllib.error.HTTPError(client.URL, code, "Error", {}, io.BytesIO(body))


def errors(*codes):
    return json.dumps(
        {"errors": [{"message": "m", "extensions": {"code": c}} for c in codes]}
    ).encode()


IN_PROGRESS, IN_REVIEW, TODO = STATES["in_progress"], STATES["in_review"], STATES["todo"]

NAMES = {
    ops.M_SUBSCRIBE: "subscribe",
    ops.Q_ISSUE_STATE: "read",
    ops.M_STATE: "move",
    ops.M_COMMENT: "comment",
    ops.M_COMMENT_ID: "comment_id",
}

FIELDS = {
    "subscribe": "issueSubscribe",
    "move": "issueUpdate",
    "comment": "commentCreate",
    "comment_id": "commentCreate",
}

CHILD = "b52be5d1-f8aa-436f-af99-3e185bd14ef5"

ISSUE = "issue-uuid"


def comment_gql(node=None, error=None):
    calls = []

    def gql(query, **v):
        calls.append((query, v))
        if error is not None:
            raise error
        return {"comment": node}

    gql.calls = calls
    return gql


def node(body="Run x", issue=ISSUE, *, me=True):
    return {"id": "c-1", "body": body, "issue": {"id": issue}, "user": {"isMe": me}}


class Gql:
    def __init__(self, state=IN_PROGRESS, fail=lambda _name, _v: None):
        self.state, self.fail, self.calls = state, fail, []

    def __call__(self, query, **v):
        name = NAMES[query]
        self.calls.append((name, v))
        r = self.fail(name, v)
        if isinstance(r, BaseException):
            raise r
        if name == "read":
            return {"issue": {"state": {"id": self.state}}}
        return {FIELDS[name]: {"success": r is not False}}


class Logs(Case):
    TS = datetime.fromisoformat("2026-10-09T00:26:33-04:00").astimezone().isoformat()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.logs = os.path.join(self.tmp, "logs")
        self.path = os.path.join(self.logs, "orchestrator.jsonl")
        log.use_logs(self.logs)

    def now(self, tz=None):
        instant = datetime.fromisoformat(self.TS)
        return instant if tz is None else instant.astimezone(tz)

    def stderr(self, call, fail=None, *, tty=False):
        err = Stderr(tty=tty, fail=fail)
        with (
            mock.patch.object(sys, "stderr", err),
        ):
            out = call()
        return out, err.getvalue()

    def text(self, path=None):
        try:
            with open(path or self.path, encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def lines(self):
        return [json.loads(line) for line in self.text().splitlines()]

    def json(self, src, kind, issue=None, **fields):
        line = {
            "ts": self.TS,
            "src": src,
            "kind": kind,
            **({"issue": issue} if issue else {}),
            **fields,
        }
        return json.dumps(line, ensure_ascii=False) + "\n"
