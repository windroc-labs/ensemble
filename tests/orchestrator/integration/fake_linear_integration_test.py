"""fake_linear.py, the orchestrator's Linear fake: its op table and seeding, then its answers over HTTP
through linear_gql (the ENSEMBLE_LINEAR seam): projection, isMe, failures, the router's board reads,
the promote, prune and write-back ops, comments by client id (FR-54, FR-55), and the issue command's
queries and mutations, alone and through LinearIssues."""

import functools
import json
import os
import time
import urllib.request
import uuid
from contextlib import redirect_stderr
from datetime import UTC, datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ensemble.orchestrator.engine import (
    types,
)
from ensemble.orchestrator.engine.flow.types import ArchiveRefused, IssueState, Linked, Priority
from ensemble.orchestrator.engine.types import AppKind, IssueError, LinearApp, LinearError
from ensemble.orchestrator.trackers.linear import (
    client,
    issue_archive,
    issue_create,
    issue_edit,
    issue_label,
    issue_list,
    issue_read,
    issue_refs,
    issue_relation,
    issue_write,
    linear_tracker,
    ops,
)
from ensemble.orchestrator.trackers.linear.issues import LinearIssues
from ensemble.utils import parse_time
from tests.orchestrator.case import Case
from tests.orchestrator.integration import fake_linear
from tests.orchestrator.integration.fake_transport import install, refuse_keychain
from tests.orchestrator.unit.board_ids import (
    HARNESS_IDENTITY,
    HARNESS_USER,
    MANAGER_IDENTITY,
    MANAGER_USER,
    STATES,
    TASK_GROUP,
    TEAM,
    role_app,
    role_id,
)
from tests.orchestrator.unit.issue_fixtures import (
    CLIENT_GROUP,
    CLIENT_LABELS,
    TASK_LABELS,
    issue_change,
    list_query,
    names_cfg,
)

HARNESS, ENGINEER = "linear-app-harness-token", "linear-app-engineer-token"

ENGINEER_USER = role_id("engineer")

PROJECT = {"id": "121166b1-191a-4461-bec4-42f1c2dc0ddd", "name": "Widgets"}

LABEL = "00000000-0000-4000-8000-000000000031"

SID = "0b6f2c1e-6d0a-4c1b-9a51-3f1f6b0e2a7d"

PR = "https://github.com/acme/widgets/pull/1"

UNKNOWN = "query { organization { id } }"

LINEAR_TIME = r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$"


class ServerCase(Case):
    def setUp(self):
        self.fake = fake = fake_linear.FakeLinear(harness=HARNESS)
        self.harness = fake.user("Harness", HARNESS, uid=HARNESS_USER)
        self.engineer = fake.user("Engineer", ENGINEER, uid=ENGINEER_USER)
        self.human = fake.user("Me", email="Me@X.com")
        fake.issue(
            "Session registry",
            identifier="TASK-7",
            state="in_progress",
            delegate=self.engineer,
            assignee=self.engineer,
            project=PROJECT,
        )
        self.url = fake_linear.serve(self, fake)
        env = mock.patch.dict(
            os.environ, {"FAKE_LINEAR_TRANSPORT": fake_linear.seam(self, fake, self.url)}
        )
        env.start()
        self.addCleanup(env.stop)
        transport = install(os.environ["FAKE_LINEAR_TRANSPORT"])
        self.addCleanup(transport.close)
        self.gql = functools.partial(client.linear_gql, identity=HARNESS_IDENTITY)
        self.eng = functools.partial(client.linear_gql, identity=role_app("engineer"))

    def log(self):
        return [(r.op, r.caller, r.result) for r in self.fake.requests]


class GraphQLProjection(ServerCase):
    def test_history_cursor_returns_the_next_page(self):
        at = datetime(2026, 9, 1, tzinfo=UTC)
        for n in range(251):
            self.fake.move(
                "TASK-7",
                "todo" if n % 2 == 0 else "in_progress",
                actor=self.human,
                at=at + timedelta(seconds=n),
            )
        first = self.gql(linear_tracker.Q_HISTORY, i="TASK-7")["issue"]["history"]
        self.assertEqual(len(first["nodes"]), 250)
        self.assertTrue(first["pageInfo"]["hasNextPage"])
        second = self.gql(
            linear_tracker.Q_HISTORY, i="TASK-7", after=first["pageInfo"]["endCursor"]
        )["issue"]["history"]
        self.assertFalse(second["pageInfo"]["hasNextPage"])
        self.assertEqual([n["createdAt"] for n in second["nodes"]], [fake_linear.stamp(at)])

    def test_projection_keeps_only_the_selection(self):
        want = {"issue": {"state": {"id": STATES["in_progress"]}}}
        self.assertEqual(self.gql(ops.Q_ISSUE_STATE, i="TASK-7"), want)
        self.assertEqual(self.gql(ops.Q_ISSUE_STATE, i=self.fake.find("TASK-7")["id"]), want)

    def test_a_selected_field_the_model_lacks_is_an_error(self):
        query = ops.Q_ISSUE_STATE.replace("state { id }", "state { id color }")
        self.fake.ops[query] = "linear.Q_ISSUE_STATE"
        with self.assertRaises(LinearError) as cm:
            self.gql(query, i="TASK-7")
        self.assertIn("fake: linear.Q_ISSUE_STATE has no field color", str(cm.exception))
        self.assertEqual(self.log(), [("linear.Q_ISSUE_STATE", HARNESS_USER, "error")])

    def test_comment_authors_by_id_and_is_me_follows_the_token(self):
        mine = {
            HARNESS: self.fake.comment("TASK-7", self.harness, f"Run {SID} · running"),
            ENGINEER: self.fake.comment("TASK-7", self.engineer, f"Run {SID} · not a session"),
        }
        self.fake.comment("TASK-7", None, "From an integration")
        for service, gql in ((HARNESS, self.gql), (ENGINEER, self.eng)):
            with self.subTest(service):
                nodes = gql(linear_tracker.Q_READ, i="TASK-7")["issue"]["comments"]["nodes"]
                self.assertEqual(
                    {n["body"]: (n["user"] or {}).get("id") for n in nodes},
                    {
                        f"Run {SID} · running": self.harness,
                        f"Run {SID} · not a session": self.engineer,
                        "From an integration": None,
                    },
                )
                self.assertEqual(
                    {
                        s: gql(ops.Q_COMMENT, c=c)["comment"]["user"]["isMe"]
                        for s, c in mine.items()
                    },
                    {HARNESS: service == HARNESS, ENGINEER: service == ENGINEER},
                )
        with self.assertRaises(LinearError):
            self.eng(linear_tracker.M_UPDATE, c=mine[HARNESS], b="hijacked")
        self.assertEqual(
            sorted(c["body"] for c in self.fake.find("TASK-7")["comments"]),
            ["From an integration", f"Run {SID} · not a session", f"Run {SID} · running"],
        )

    def test_a_session_comment_is_created_then_updated_by_its_session_id(self):
        session = ops.client_id(SID, "session")
        for text in (f"Run {SID} · running · #1", f"Run {SID} · done · #2"):
            self.assertTrue(
                linear_tracker.LinearTracker(self.gql).put_comment("TASK-7", text, (SID, "session"))
            )
        self.assertEqual(
            [(c["id"], c["user"], c["body"]) for c in self.fake.find("TASK-7")["comments"]],
            [(session, self.harness, f"Run {SID} · done · #2")],
        )
        self.assertEqual(
            self.log(),
            [
                ("sessions.M_UPDATE", HARNESS_USER, "error"),
                ("linear.M_COMMENT_ID", HARNESS_USER, "ok"),
                ("sessions.M_UPDATE", HARNESS_USER, "ok"),
            ],
        )

    def test_a_comment_is_created_and_found_by_its_client_id(self):
        cid = ops.client_id(SID, "comment")
        task, other = self.fake.find("TASK-7")["id"], self.fake.issue("Other")["id"]
        self.assertIsNone(ops.find_comment(self.eng, cid, task))
        ops.comment(self.eng, task, "Build ready.", cid)
        self.assertEqual(
            self.eng(ops.Q_COMMENT, c=cid),
            {
                "comment": {
                    "id": cid,
                    "body": "Build ready.",
                    "issue": {"id": task},
                    "user": {"isMe": True},
                }
            },
        )
        self.assertEqual(ops.find_comment(self.eng, cid, task), "Build ready.")
        for gql, issue in ((self.gql, task), (self.eng, other)):
            with self.subTest(issue=issue), self.assertRaisesRegex(LinearError, "is not ours$"):
                ops.find_comment(gql, cid, issue)
        with self.assertRaises(LinearError) as cm:
            ops.comment(self.gql, other, "Squatted.", cid)
        self.assertIn(f"fake: comment id {cid} in use", str(cm.exception))
        self.assertEqual(
            [(c["id"], c["body"]) for c in self.fake.find("TASK-7")["comments"]],
            [(cid, "Build ready.")],
        )
        self.assertEqual(self.fake.find(other)["comments"], [])
        self.assertEqual(
            self.log(),
            [
                ("linear.Q_COMMENT", ENGINEER_USER, "error"),
                ("linear.M_COMMENT_ID", ENGINEER_USER, "ok"),
                *[("linear.Q_COMMENT", ENGINEER_USER, "ok")] * 2,
                ("linear.Q_COMMENT", HARNESS_USER, "ok"),
                ("linear.Q_COMMENT", ENGINEER_USER, "ok"),
                ("linear.M_COMMENT_ID", HARNESS_USER, "error"),
            ],
        )

    def test_a_move_is_history_by_the_caller(self):
        self.fake.issue("Claimed", identifier="TASK-8")
        self.fake.move(
            "TASK-8",
            "in_progress",
            actor=self.harness,
            at=datetime(2026, 10, 8, 7, 30, 41, 62000, UTC),
        )
        self.assertIsNone(ops.move(self.eng, "TASK-8", STATES["in_review"], STATES["in_progress"]))
        nodes = self.gql(linear_tracker.Q_DETAIL, i="TASK-8")["issue"]["history"]["nodes"]
        self.assertEqual(
            [(n["actorId"], n["fromStateId"], n["toStateId"]) for n in nodes],
            [
                (self.engineer, STATES["in_progress"], STATES["in_review"]),
                (self.harness, STATES["todo"], STATES["in_progress"]),
            ],
        )
        self.assertEqual(nodes[1]["createdAt"], "2026-10-08T07:30:41.062Z")
        self.assertRegex(nodes[0]["createdAt"], LINEAR_TIME)
        self.assertLess(
            datetime.now(UTC) - parse_time(nodes[0]["createdAt"]), timedelta(seconds=10)
        )
        self.assertEqual(self.fake.find("TASK-8")["state"], STATES["in_review"])

    def test_each_failure_kind_as_the_caller_sees_it(self):
        for name, eng in (
            ("http", self.eng),
            ("in process", functools.partial(self.fake, identity=role_app("engineer"))),
        ):
            with self.subTest(name):
                self.fake.requests.clear()
                self.fake.fail("linear.M_COMMENT", "error")
                with self.assertRaises(LinearError) as cm:
                    ops.comment(eng, "TASK-7", "lost")
                self.assertIsInstance(str(cm.exception), str)
                self.fake.fail("linear.M_STATE", "unsuccessful")
                with self.assertRaisesRegex(LinearError, "^issueUpdate: success: false$"):
                    ops.move(eng, "TASK-7", STATES["in_review"], STATES["in_progress"])
                self.fake.fail("linear.M_SUBSCRIBE", "503")
                with self.assertRaises(types.Unavailable) as cm:
                    ops.call(eng, ops.M_SUBSCRIBE, "issueSubscribe", i="TASK-7", e="me@x.com")
                self.assertEqual(str(cm.exception), "issueSubscribe: HTTP 503")
                self.fake.fail("linear.Q_ISSUE_STATE", "hang", seconds=3)
                start = time.monotonic()
                with self.assertRaises(types.Unavailable) as cm:
                    eng(ops.Q_ISSUE_STATE, timeout=1, i="TASK-7")
                self.assertEqual(str(cm.exception), "issue: TimeoutError: timed out")
                self.assertLess(time.monotonic() - start, 2.5)
                self.assertEqual(
                    self.log(),
                    [
                        ("linear.M_COMMENT", ENGINEER_USER, "error"),
                        ("linear.Q_ISSUE_STATE", ENGINEER_USER, "ok"),
                        ("linear.M_STATE", ENGINEER_USER, "unsuccessful"),
                        ("linear.M_SUBSCRIBE", ENGINEER_USER, "503"),
                        ("linear.Q_ISSUE_STATE", ENGINEER_USER, "hang"),
                    ],
                )
        task = self.fake.find("TASK-7")
        self.assertEqual(
            (task["state"], task["comments"], task["subscribers"]), (STATES["in_progress"], [], [])
        )

    def test_caller_and_times_scope_a_failure(self):
        self.fake.fail("linear.M_COMMENT", "error", caller=ENGINEER_USER, times=2)
        ops.comment(self.gql, "TASK-7", "by the harness")
        for _ in range(2):
            with self.assertRaises(LinearError):
                ops.comment(self.eng, "TASK-7", "lost")
        ops.comment(self.eng, "TASK-7", "by the engineer")
        self.assertEqual(
            self.log(),
            [
                ("linear.M_COMMENT", HARNESS_USER, "ok"),
                ("linear.M_COMMENT", ENGINEER_USER, "error"),
                ("linear.M_COMMENT", ENGINEER_USER, "error"),
                ("linear.M_COMMENT", ENGINEER_USER, "ok"),
            ],
        )
        self.assertEqual(
            [c["body"] for c in self.fake.find("TASK-7")["comments"]],
            ["by the harness", "by the engineer"],
        )

    def test_an_unknown_query_and_a_bad_token_are_errors_and_logged(self):
        with self.assertRaises(LinearError) as cm:
            self.gql(UNKNOWN)
        self.assertIn("fake: unknown query", str(cm.exception))
        stranger = fake_linear.FakeLinear()
        uid = stranger.user("Stranger", "stranger")
        app = LinearApp(
            "stranger",
            AppKind.ROLE,
            "linear.roles.stranger",
            "c",
            "stranger-secret",
            "stranger",
            uid,
        )
        with (
            mock.patch.dict(
                os.environ, {"FAKE_LINEAR_TRANSPORT": fake_linear.seam(self, stranger, self.url)}
            ),
            self.assertRaises(LinearError) as cm,
        ):
            client.linear_gql(ops.Q_TEAM, identity=app, t=TEAM)
        self.assertEqual(str(cm.exception), "linear api error: teams: HTTP 401")
        self.assertEqual(
            self.fake.requests,
            [
                fake_linear.Request(UNKNOWN, HARNESS_USER, {}, "unknown"),
                fake_linear.Request("linear.Q_TEAM", None, {"t": TEAM}, "unauthorized"),
            ],
        )

    def test_only_post_graphql_is_served(self):
        send = urllib.request.build_opener(urllib.request.ProxyHandler({})).open
        for req, code in (
            (self.url, 405),
            (urllib.request.Request(self.url.replace("/graphql", "/other"), b"{}"), 404),
        ):
            with self.subTest(code), self.assertRaises(urllib.error.HTTPError) as cm:
                send(req, timeout=5)
            cm.exception.close()
            self.assertEqual(cm.exception.code, code)
        self.assertEqual(self.fake.requests, [])

    def test_a_bad_call_is_a_logged_error(self):
        with self.assertRaises(LinearError) as cm:
            self.gql(ops.Q_ISSUE_STATE)
        self.assertIn("fake: linear.Q_ISSUE_STATE: KeyError: 'i'", str(cm.exception))
        with self.assertRaises(LinearError) as cm:
            self.gql(linear_tracker.Q_HANDOFF, t=TEAM, s=STATES["in_progress"], a=None)
        self.assertIn("fake: promote.Q_HANDOFF: TypeError: ", str(cm.exception))
        self.assertEqual(
            self.log(),
            [
                ("linear.Q_ISSUE_STATE", HARNESS_USER, "error"),
                ("promote.Q_HANDOFF", HARNESS_USER, "error"),
            ],
        )

    def test_a_bad_body_is_a_logged_400(self):
        send, authorization = (
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open,
            f"Bearer {fake_linear._token(HARNESS)}",
        )
        for body in (b"not json", b"[]", b'{"variables": {}}', b'{"query": "q", "variables": []}'):
            with self.subTest(body), self.assertRaises(urllib.error.HTTPError) as cm:
                send(
                    urllib.request.Request(self.url, body, {"Authorization": authorization}),
                    timeout=5,
                )
            cm.exception.close()
            self.assertEqual(cm.exception.code, 400)
        self.assertEqual(
            self.fake.requests, [fake_linear.Request(None, HARNESS_USER, {}, "error")] * 4
        )

    def test_only_a_bearer_token_is_authorized(self):
        send, token = (
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open,
            fake_linear._token(HARNESS),
        )
        body = json.dumps({"query": ops.Q_ISSUE_STATE, "variables": {"i": "TASK-7"}}).encode()

        def post(authorization):
            request = urllib.request.Request(self.url, body, {"Authorization": authorization})
            return send(request, timeout=5)

        for authorization in (token, f"bearer {token}"):
            with self.subTest(authorization), self.assertRaises(urllib.error.HTTPError) as cm:
                post(authorization)
            answer = cm.exception.read().decode()
            cm.exception.close()
            self.assertEqual(cm.exception.code, 401)
            self.assertEqual(json.loads(answer), fake_linear._errors("fake: unauthorized"))
            self.assertNotIn(token, answer)
        with post(f"Bearer {token}") as res:
            self.assertEqual(
                json.load(res), {"data": {"issue": {"state": {"id": STATES["in_progress"]}}}}
            )
        unauthorized = fake_linear.Request(
            "linear.Q_ISSUE_STATE", None, {"i": "TASK-7"}, "unauthorized"
        )
        self.assertEqual(
            self.fake.requests,
            [
                unauthorized,
                unauthorized,
                fake_linear.Request("linear.Q_ISSUE_STATE", HARNESS_USER, {"i": "TASK-7"}, "ok"),
            ],
        )

    def test_finished_issues_page(self):
        finished = [
            self.fake.issue(
                f"Finished {n}",
                state=("done", "canceled")[n % 2],
                delegate=self.engineer,
                assignee=self.human,
            )["identifier"]
            for n in range(51)
        ]
        archived = self.fake.issue(
            "Archived", state="done", delegate=self.engineer, assignee=self.human
        )["identifier"]
        ops.call(self.gql, linear_tracker.M_ARCHIVE, "issueArchive", i=archived)
        self.fake.issue("Someone else's", state="done", assignee=self.human)
        self.fake.issue("Role assignee only", state="done", assignee=self.engineer)
        self.fake.issue(
            "Foreign delegate", state="done", delegate=self.human, assignee=self.engineer
        )
        pages, cursor = [], None
        while True:
            page = self.gql(
                linear_tracker.Q_FINISHED,
                t=TEAM,
                d=[STATES["done"]],
                s=[STATES["canceled"]],
                a=[self.engineer],
                c=cursor,
            )["issues"]
            pages.append([n["identifier"] for n in page["nodes"]])
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]
        self.assertEqual([len(p) for p in pages], [50, 1])
        self.assertEqual(sum(pages, []), finished)
        self.assertTrue(self.fake.find(archived)["archived"])

    def test_create_with_delegate_defaults_omitted_assignee_to_caller_but_keeps_explicit_null(self):
        for assignment, want in (({}, self.harness), ({"assigneeId": None}, None)):
            with self.subTest(assignment=assignment):
                new = {
                    "id": str(uuid.uuid4()),
                    "teamId": TEAM,
                    "delegateId": self.engineer,
                    "title": "Delegated work",
                    **assignment,
                }
                ops.call(self.gql, linear_tracker.M_CREATE, "issueCreate", **{"in": new})
                made = self.fake.find(new["id"])
                self.assertEqual((made["delegate"], made["assignee"]), (self.engineer, want))

    def test_board_filters_and_projects_delegate_independently_of_assignee(self):
        delegated = self.fake.issue(
            "Delegated", delegate=self.engineer, assignee=self.human, project=PROJECT
        )
        self.fake.issue("Only assigned", assignee=self.engineer, project=PROJECT)
        page = self.gql(
            linear_tracker.q_issues(),
            f={
                "delegate": {"id": {"eq": self.engineer}},
                "state": {"id": {"eq": STATES["todo"]}},
            },
        )["issues"]
        self.assertEqual([n["id"] for n in page["nodes"]], [delegated["id"]])
        self.assertEqual(page["nodes"][0]["delegate"], {"id": self.engineer})
        checked = self.gql(linear_tracker.Q_RECHECK, i=delegated["id"])["issue"]
        self.assertEqual(checked["delegate"], {"id": self.engineer})
        self.assertEqual(checked["assignee"]["id"], self.human)

    def test_board_cursor_returns_the_next_page(self):
        seeded = [self.fake.issue(f"Ready {n}")["id"] for n in range(101)]
        query = linear_tracker.q_issues()
        flt = {"state": {"id": {"eq": STATES["todo"]}}}
        first = self.gql(query, f=flt)["issues"]
        self.assertEqual([n["id"] for n in first["nodes"]], seeded[:100])
        self.assertTrue(first["pageInfo"]["hasNextPage"])
        second = self.gql(query, f=flt, c=first["pageInfo"]["endCursor"])["issues"]
        self.assertEqual([n["id"] for n in second["nodes"]], seeded[100:])
        self.assertFalse(second["pageInfo"]["hasNextPage"])

    def test_issue_create_takes_the_next_number_and_refuses_a_used_id(self):
        self.fake.issue("Older", identifier="TASK-3")
        new = {
            "id": ops.client_id("src", "pm", "2026-10-09T07:00:00.000Z"),
            "teamId": TEAM,
            "projectId": PROJECT["id"],
            "delegateId": self.engineer,
            "assigneeId": None,
            "stateId": STATES["todo"],
            "priority": 2,
            "title": "PRD: Widgets",
            "description": "Handoff from TASK-7: https://linear.app/fake/issue/TASK-7",
        }
        made = ops.call(self.gql, linear_tracker.M_CREATE, "issueCreate", **{"in": new})
        self.assertEqual(
            made, {"success": True, "issue": {"id": new["id"], "identifier": "TASK-8"}}
        )
        self.assertEqual(
            self.gql(linear_tracker.Q_CHILD, c=new["id"]), {"issues": {"nodes": [made["issue"]]}}
        )
        child = self.fake.find("TASK-8")
        self.assertEqual(
            {
                k: child[k]
                for k in (
                    "id",
                    "state",
                    "delegate",
                    "assignee",
                    "project",
                    "priority",
                    "title",
                    "description",
                )
            },
            {
                "id": new["id"],
                "state": STATES["todo"],
                "delegate": self.engineer,
                "assignee": None,
                "project": PROJECT,
                "priority": 2,
                "title": "PRD: Widgets",
                "description": new["description"],
            },
        )
        with self.assertRaises(LinearError) as cm:
            ops.call(
                self.gql, linear_tracker.M_CREATE, "issueCreate", **{"in": dict(new, title="Again")}
            )
        self.assertIn(new["id"], str(cm.exception))
        self.assertEqual([r.result for r in self.fake.requests], ["ok", "ok", "error"])
        self.assertIsNone(self.fake.find("TASK-9"))

    def test_the_board_reads(self):
        done = "2026-10-08T07:00:00.000Z"
        self.fake.label_group(TASK_GROUP, "Tasks", {LABEL: "build"})
        blocker = self.fake.issue("Blocker", state="in_progress")
        ready = self.fake.issue(
            "Ready",
            delegate=self.engineer,
            assignee=self.human,
            project=PROJECT,
            labels=[LABEL],
        )
        self.fake.issue("No project", delegate=self.engineer, assignee=self.human)
        self.fake.issue("Someone else's", assignee=self.human, project=PROJECT)
        ops.call(
            self.gql,
            linear_tracker.M_RELATE,
            "issueRelationCreate",
            **{"in": {"type": "blocks", "issueId": blocker["id"], "relatedIssueId": ready["id"]}},
        )
        cfg = SimpleNamespace(
            team=TEAM, states=STATES, task_label_group=TASK_GROUP, task_labels={"build": LABEL}
        )
        self.assertEqual(ops.team(self.gql, cfg).states, STATES)
        ops.task_group(self.gql, cfg)
        flt = {
            "team": {"id": {"eq": TEAM}},
            "project": {"null": False},
            "delegate": {"id": {"in": [self.engineer]}},
            "state": {"id": {"eq": STATES["todo"]}},
        }
        nodes = self.gql(linear_tracker.q_issues(linear_tracker.RELATIONS), f=flt)["issues"][
            "nodes"
        ]
        self.assertEqual([n["identifier"] for n in nodes], [ready["identifier"]])
        self.assertEqual(linear_tracker.blockers(nodes[0]), [blocker["identifier"]])
        self.assertNotIn(
            "inverseRelations", self.gql(linear_tracker.q_issues(), f=flt)["issues"]["nodes"][0]
        )
        self.assertEqual(
            self.gql(linear_tracker.Q_RECHECK, i=ready["id"])["issue"],
            {
                "state": {"id": STATES["todo"]},
                "delegate": {"id": self.engineer},
                "assignee": {"id": self.human, "email": "Me@X.com"},
                "project": PROJECT,
                "labels": {"nodes": [{"id": LABEL, "name": "build", "parent": {"id": TASK_GROUP}}]},
                "archivedAt": None,
            },
        )
        self.fake.move(blocker["id"], "done", actor=self.harness, at=done)
        self.assertEqual(
            linear_tracker.blockers(self.gql(linear_tracker.Q_RELATIONS, i=ready["id"])["issue"]),
            [],
        )
        self.assertEqual(
            self.gql(linear_tracker.Q_HISTORY, i=blocker["id"])["issue"]["history"]["nodes"],
            [
                {
                    "createdAt": done,
                    "toStateId": STATES["done"],
                    "actor": {"id": self.harness},
                    "botActor": None,
                }
            ],
        )


class FixtureOperations(Case):
    def test_every_query_constant_has_an_op(self):
        want = [
            value
            for module in (ops, linear_tracker)
            for key, value in vars(module).items()
            if key[:2] in ("Q_", "M_") and isinstance(value, str)
        ]
        want += [linear_tracker.q_issues(), linear_tracker.q_issues(linear_tracker.RELATIONS)]
        want += [
            issue_read.Q_READ,
            issue_read.Q_FULL,
            *issue_read.MORE.values(),
            issue_list.Q_LIST,
            issue_create.M_CREATE,
            issue_create.M_RELATE,
            issue_create.Q_VIEWER,
            issue_refs.Q_ISSUE,
            issue_edit.Q_READ,
            issue_edit.M_EDIT,
            issue_label.Q_MORE,
            issue_write.Q_MOVE,
            issue_write.Q_COMMENT,
            issue_write.M_COMMENT,
            issue_archive.Q_ISSUE,
            *issue_relation.Q_READ.values(),
            *issue_relation.Q_MORE.values(),
            issue_relation.M_ADD,
            issue_relation.M_REMOVE,
        ]
        self.assertCountEqual(fake_linear.texts().values(), dict.fromkeys(want))

    def test_a_dict_constant_names_its_key(self):
        texts = fake_linear.texts()
        self.assertEqual(texts["issue_read.MORE[comments]"], issue_read.MORE["comments"])
        self.assertEqual(
            texts["issue_relation.Q_READ[blocked_by]"], issue_relation.Q_READ["blocked_by"]
        )

    def test_two_names_for_one_text_are_refused(self):
        with self.assertRaisesRegex(ValueError, "a.X, b.Y"):
            fake_linear.index({"a.X": "q", "b.Y": "q"})

    def test_fail_refuses_a_misuse(self):
        fake = fake_linear.FakeLinear()
        engineer = fake.user("Engineer", "linear-app-engineer-token", uid=ENGINEER_USER)
        human = fake.user("Me", email="me@x.com")
        for args, kw in (
            (("linear.M_NOPE", "error"), {}),
            (("linear.M_STATE", "500"), {}),
            (("linear.Q_ISSUE_STATE", "unsuccessful"), {}),
            (("linear.M_STATE", "error"), {"times": 0}),
            (("linear.M_STATE", "error"), {"caller": "nobody"}),
            (("linear.M_STATE", "error"), {"caller": human}),
        ):
            with self.subTest(args, **kw), self.assertRaises(ValueError):
                fake.fail(*args, **kw)
        fake.fail("linear.M_STATE", "error", caller=engineer)

    def test_seeding_refuses_a_user_twice(self):
        fake = fake_linear.FakeLinear()
        fake.user("Engineer", "linear-app-engineer-token", uid=ENGINEER_USER)
        fake.user("Me", email="me@x.com")
        for args, kw in (
            (("Other",), {"uid": ENGINEER_USER}),
            (("Other", "linear-app-engineer-token"), {}),
            (("Other",), {"email": "ME@x.com"}),
        ):
            with self.subTest(args, **kw), self.assertRaises(ValueError):
                fake.user(*args, **kw)
        self.assertEqual(len(fake.users), 2)
        self.assertEqual(
            fake.tokens(),
            {"linear-app-engineer-token": fake_linear._record("linear-app-engineer-token")},
        )

    def test_seeding_refuses_a_used_or_malformed_issue_id(self):
        fake = fake_linear.FakeLinear()
        uid = fake.issue(identifier="TASK-7")["id"]
        for kw in ({"identifier": "TASK-7"}, {"id": uid}, {"identifier": "OTHER-1"}):
            with self.subTest(**kw), self.assertRaises(ValueError):
                fake.issue(**kw)

    def test_seeding_a_parent_takes_a_seeded_issue(self):
        fake = fake_linear.FakeLinear()
        parent = fake.issue("Parent", identifier="TASK-1")
        self.assertEqual(fake.issue("Child", parent="TASK-1")["parent"], parent["id"])
        self.assertIsNone(fake.find("TASK-1")["parent"])
        with self.assertRaises(ValueError):
            fake.issue("Orphan", parent="TASK-9")
        self.assertIsNone(fake.find("TASK-3"))

    def test_backlog_is_a_configured_state(self):
        self.assertEqual(fake_linear.BACKLOG, STATES["backlog"])
        self.assertEqual(
            (fake_linear.NAMES[fake_linear.BACKLOG], fake_linear.TYPES["backlog"]),
            ("backlog", "backlog"),
        )

    def test_a_backlog_issue_moves_to_todo_and_back(self):
        fake = fake_linear.FakeLinear()
        actor = fake.user("Me", email="me@x.com")
        self.assertEqual(
            fake.issue("Later", identifier="TASK-1", state="backlog")["state"],
            fake_linear.BACKLOG,
        )
        fake.move("TASK-1", "todo", actor=actor)
        self.assertEqual(fake.find("TASK-1")["state"], STATES["todo"])
        fake.move("TASK-1", "backlog", actor=actor)
        task = fake.find("TASK-1")
        self.assertEqual(task["state"], fake_linear.BACKLOG)
        self.assertEqual(
            [
                (fake_linear.NAMES[h["fromStateId"]], fake_linear.NAMES[h["toStateId"]])
                for h in task["history"]
            ],
            [("backlog", "todo"), ("todo", "backlog")],
        )

    def test_relate_records_the_issues_ids_and_refuses_an_unknown_one(self):
        fake = fake_linear.FakeLinear()
        a, b = (
            fake.issue("A", identifier="TASK-1"),
            fake.issue("B", identifier="TASK-2"),
        )
        fake.relate("blocks", "TASK-1", b["id"])
        self.assertEqual(fake.relations, [("blocks", a["id"], b["id"])])
        for refs in (("TASK-1", "TASK-9"), ("TASK-9", "TASK-2"), (a["id"], "no-such-id")):
            with self.subTest(refs), self.assertRaises(ValueError):
                fake.relate("blocks", *refs)
        self.assertEqual(len(fake.relations), 1)

    def test_a_time_without_an_offset_is_utc(self):
        tz = mock.patch.dict(os.environ, {"TZ": "Asia/Tokyo"})
        self.addCleanup(time.tzset)
        tz.start()
        self.addCleanup(tz.stop)
        time.tzset()
        for t in (
            datetime(2026, 10, 9, 7, 30, 41, 62000),
            "2026-10-09T07:30:41.062",
            "2026-10-09T07:30:41.062Z",
            "2026-10-09T16:30:41.062+09:00",
            datetime(2026, 10, 9, 16, 30, 41, 62999, timezone(timedelta(hours=9))),
        ):
            with self.subTest(t):
                self.assertEqual(fake_linear.stamp(t), "2026-10-09T07:30:41.062Z")


class GraphQLQueries(ServerCase):
    def test_the_team_lists_backlog_beside_the_config_states(self):
        query = ops.Q_TEAM.replace(
            "states(first: 100) { nodes { id type } }",
            "states(first: 100) { nodes { id name type } }",
        )
        self.fake.ops[query] = "linear.Q_TEAM"
        nodes = self.gql(query, t=TEAM)["teams"]["nodes"][0]["states"]["nodes"]
        self.assertEqual(
            {n["id"]: (n["name"], n["type"]) for n in nodes},
            {
                STATES["todo"]: ("Todo", "unstarted"),
                STATES["in_progress"]: ("In Progress", "started"),
                STATES["in_review"]: ("In Review", "started"),
                STATES["handoff"]: ("Handoff", "started"),
                STATES["done"]: ("Done", "completed"),
                STATES["canceled"]: ("Canceled", "canceled"),
                fake_linear.BACKLOG: ("Backlog", "backlog"),
            },
        )
        self.assertEqual(len(nodes), 7)
        cfg = SimpleNamespace(team=TEAM, states=STATES)
        self.assertEqual(ops.team(self.gql, cfg).states, STATES)

    def test_a_backlog_issue_is_not_in_a_todo_list(self):
        later = self.fake.issue(
            "Later",
            state="backlog",
            delegate=self.engineer,
            project=PROJECT,
        )
        ready = self.fake.issue("Ready", delegate=self.engineer, project=PROJECT)

        def listed(state):
            flt = {
                "team": {"id": {"eq": TEAM}},
                "project": {"null": False},
                "delegate": {"id": {"in": [self.engineer]}},
                "state": {"id": {"eq": state}},
            }
            return [
                n["identifier"]
                for n in self.gql(linear_tracker.q_issues(), f=flt)["issues"]["nodes"]
            ]

        backlog = fake_linear.BACKLOG
        self.assertEqual(listed(STATES["todo"]), [ready["identifier"]])
        self.assertEqual(listed(backlog), [later["identifier"]])
        self.assertIsNone(ops.move(self.gql, later["id"], STATES["todo"], backlog))
        self.assertEqual(listed(STATES["todo"]), [later["identifier"], ready["identifier"]])
        self.assertIsNone(ops.move(self.gql, ready["id"], backlog, STATES["todo"]))
        self.assertEqual(
            (listed(STATES["todo"]), listed(backlog)),
            ([later["identifier"]], [ready["identifier"]]),
        )
        self.assertEqual(
            self.gql(ops.Q_ISSUE_STATE, i=ready["id"]), {"issue": {"state": {"id": backlog}}}
        )

    def test_issue_create_takes_the_backlog_state(self):
        new = {
            "id": str(uuid.uuid4()),
            "teamId": TEAM,
            "stateId": fake_linear.BACKLOG,
            "title": "Later",
        }
        ops.call(self.gql, linear_tracker.M_CREATE, "issueCreate", **{"in": new})
        self.assertEqual(self.fake.find(new["id"])["state"], fake_linear.BACKLOG)

    def test_relate_shows_in_relations_and_inverse_relations(self):
        self.fake.issue("Later", identifier="TASK-8", state="backlog")
        ready = self.fake.issue("Ready", identifier="TASK-9")
        self.fake.relate("blocks", "TASK-8", ready["id"])
        self.assertEqual(
            self.gql(linear_tracker.Q_READ, i="TASK-8")["issue"]["relations"]["nodes"],
            [
                {
                    "relatedIssue": {
                        "identifier": "TASK-9",
                        "title": "Ready",
                        "state": {"name": "Todo"},
                    }
                }
            ],
        )
        self.assertEqual(
            self.gql(linear_tracker.Q_READ, i="TASK-9")["issue"]["inverseRelations"]["nodes"],
            [{"issue": {"identifier": "TASK-8", "title": "Later", "state": {"name": "Backlog"}}}],
        )

    def test_the_write_back_ops(self):
        prd = self.fake.issue("PRD: Widgets", state="done")
        ops.call(
            self.gql,
            linear_tracker.M_RELATE,
            "issueRelationCreate",
            **{"in": {"type": "related", "issueId": prd["id"], "relatedIssueId": "TASK-7"}},
        )
        for _ in range(2):
            ops.call(
                self.eng, linear_tracker.M_ATTACH, "attachmentLinkURL", i="TASK-7", u=PR, t="PR"
            )
        self.assertEqual(
            self.eng(linear_tracker.Q_ATTACHED, i="TASK-7"),
            {"issue": {"attachments": {"nodes": [{"url": PR}]}}},
        )
        self.assertEqual(ops.subscribe(self.eng, "TASK-7", ["ME@x.com"]), "")
        linear_tracker.LinearTracker(self.eng).assign("TASK-7", self.human)
        self.assertEqual(self.fake.find("TASK-7")["assignee"], self.human)
        with self.assertRaisesRegex(LinearError, "Entity not found: User"):
            linear_tracker.LinearTracker(self.eng).assign("TASK-7", "no-such-user")
        ops.call(
            self.eng, linear_tracker.M_TITLE, "issueUpdate", i="TASK-7", t="ENG: Session registry"
        )
        task = self.fake.find("TASK-7")
        with self.fake.lock:
            task["archived"] = True
        self.assertEqual(
            self.gql(linear_tracker.Q_ID, i="TASK-7"),
            {"issue": {"id": task["id"], "team": {"id": TEAM}}},
        )
        read = linear_tracker.LinearTracker(
            self.gql, selves=linear_tracker.Selves(self.harness, frozenset())
        ).issue("TASK-7")
        self.assertEqual(
            (read.title, read.home, read.linked),
            (
                "ENG: Session registry",
                PROJECT["id"],
                (Linked(prd["identifier"], "PRD: Widgets", "Done"),),
            ),
        )
        ops.call(self.eng, linear_tracker.M_UNARCHIVE, "issueUnarchive", i=task["id"])
        self.assertEqual(
            (task["attachments"], task["subscribers"], task["archived"]),
            ([{"url": PR, "title": "PR"}], ["Me@X.com"], False),
        )

    def test_the_promote_and_prune_reads(self):
        created, handed, said, done = (f"2026-10-0{d}T07:00:00.000Z" for d in (5, 6, 7, 8))
        src = self.fake.issue(
            "Research",
            identifier="TASK-9",
            state="in_review",
            delegate=self.engineer,
            assignee=self.human,
            project=PROJECT,
            priority=2,
            created=created,
        )
        self.fake.move("TASK-9", "handoff", actor=self.human, at=handed)
        self.fake.comment("TASK-9", self.human, "Build it.", at=said)
        ops.call(self.eng, linear_tracker.M_ATTACH, "attachmentLinkURL", i="TASK-9", u=PR, t="PR")
        ops.call(
            self.gql,
            linear_tracker.M_RELATE,
            "issueRelationCreate",
            **{"in": {"type": "related", "issueId": "TASK-7", "relatedIssueId": "TASK-9"}},
        )
        self.assertEqual(
            self.gql(linear_tracker.Q_HANDOFF, t=TEAM, s=STATES["handoff"], a=[self.engineer])[
                "issues"
            ]["nodes"],
            [
                {
                    "id": src["id"],
                    "identifier": "TASK-9",
                    "url": src["url"],
                    "title": "Research",
                    "priority": 2,
                    "createdAt": created,
                    "project": PROJECT,
                    "delegate": {"id": self.engineer},
                    "attachments": {"nodes": [{"title": "PR", "url": PR}]},
                }
            ],
        )
        self.assertEqual(
            self.gql(linear_tracker.Q_DETAIL, i=src["id"])["issue"],
            {
                "state": {"id": STATES["handoff"]},
                "history": {
                    "nodes": [
                        {
                            "createdAt": handed,
                            "actorId": self.human,
                            "fromStateId": STATES["in_review"],
                            "toStateId": STATES["handoff"],
                        }
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
                "comments": {
                    "nodes": [
                        {
                            "body": "Build it.",
                            "createdAt": said,
                            "user": {"id": self.human, "email": "Me@X.com", "name": "Me"},
                        }
                    ]
                },
                "relations": {"nodes": []},
                "inverseRelations": {"nodes": [{"issue": {"id": self.fake.find("TASK-7")["id"]}}]},
            },
        )
        self.fake.move("TASK-9", "done", actor=self.harness, at=done)
        finished = self.gql(linear_tracker.Q_ISSUE, i="TASK-9")["issue"]
        self.assertEqual(finished["state"], {"id": STATES["done"], "type": "completed"})
        self.assertEqual(
            ops.last_move(finished["history"]["nodes"], {STATES["done"]}),
            parse_time(done),
        )


def pages(page):
    return {"hasNextPage": False, "endCursor": page[-1]["id"] if page else None}


class IssueOps(ServerCase):
    """The issue command's texts, each alone, then through LinearIssues as the command runs them."""

    def setUp(self):
        super().setUp()
        self.fake.label_group(TASK_GROUP, "Tasks", {i: n for n, i in TASK_LABELS.items()})
        self.fake.label_group(CLIENT_GROUP, "Clients", {i: n for n, i in CLIENT_LABELS.items()})
        self.task7 = self.fake.find("TASK-7")
        self.issues = LinearIssues(self.gql, names_cfg())
        self.me = {"id": self.harness, "name": "Harness"}
        self.manager = self.fake.user("Manager", MANAGER_IDENTITY.token_service, uid=MANAGER_USER)

    def ops(self):
        return [r.op for r in self.fake.requests]

    def listed(self, **given):
        return sorted(row[0]["identifier"] for row in self.issues.walk(list_query(**given)))

    def test_the_viewer(self):
        self.assertEqual(self.gql(issue_create.Q_VIEWER), {"viewer": self.me})
        self.assertEqual(self.ops(), ["issue_create.Q_VIEWER"])

    def test_an_issue_by_identifier_or_id(self):
        want = {"issue": {"id": self.task7["id"], "identifier": "TASK-7"}}
        for ref in ("TASK-7", self.task7["id"]):
            self.assertEqual(self.gql(issue_refs.Q_ISSUE, id=ref), want)
        with self.assertRaisesRegex(LinearError, "Entity not found: Issue"):
            self.gql(issue_refs.Q_ISSUE, id="TASK-99")

    def test_create_takes_parent_labels_assignee_priority_and_state(self):
        new = {
            "teamId": TEAM,
            "title": "Child",
            "description": "Body",
            "stateId": STATES["backlog"],
            "labelIds": [TASK_LABELS["build"], CLIENT_LABELS["codex"]],
            "parentId": self.task7["id"],
            "assigneeId": self.engineer,
            "priority": 2,
        }
        made = ops.call(self.gql, issue_create.M_CREATE, "issueCreate", **{"in": new})
        child = self.fake.find("TASK-8")
        self.assertEqual(
            made,
            {
                "success": True,
                "issue": {"id": child["id"], "identifier": "TASK-8", "url": child["url"]},
            },
        )
        self.assertEqual(
            {
                k: child[k]
                for k in ("parent", "labels", "assignee", "priority", "state", "description")
            },
            {
                "parent": self.task7["id"],
                "labels": new["labelIds"],
                "assignee": self.engineer,
                "priority": 2,
                "state": STATES["backlog"],
                "description": "Body",
            },
        )
        for name, bad, error in (
            ("an unknown input", {"cycleId": TEAM}, "fake: issueCreate has no input cycleId"),
            ("a label group", {"labelIds": [TASK_GROUP]}, "Entity not found: IssueLabel"),
            ("an unknown label", {"labelIds": [PROJECT["id"]]}, "Entity not found: IssueLabel"),
            (
                "two labels of a group",
                {"labelIds": list(TASK_LABELS.values())},
                f"fake: two labels of group {TASK_GROUP}",
            ),
            ("an unknown parent", {"parentId": PROJECT["id"]}, "Entity not found: Issue"),
        ):
            with self.subTest(name), self.assertRaisesRegex(LinearError, error):
                ops.call(self.gql, issue_create.M_CREATE, "issueCreate", **{"in": new | bad})
        self.assertIsNone(self.fake.find("TASK-9"))

    def test_read_shows_parent_children_and_comment_fields(self):
        child = self.fake.issue("Child", parent="TASK-7")
        said = "2026-10-09T07:00:00.000Z"
        cid = self.fake.comment("TASK-7", self.human, "Split it.", at=said)
        parent = self.gql(issue_read.Q_READ, i="TASK-7")["issue"]
        brief = {
            "id": child["id"],
            "identifier": "TASK-8",
            "title": "Child",
            "state": {"name": "Todo"},
        }
        self.assertEqual(parent["children"], {"nodes": [brief], "pageInfo": pages([brief])})
        self.assertIsNone(parent["parent"])
        self.assertIsNone(parent["projectMilestone"])
        self.assertEqual(
            parent["comments"]["nodes"],
            [
                {
                    "id": cid,
                    "body": "Split it.",
                    "createdAt": said,
                    "parentId": None,
                    "quotedText": None,
                    "user": {"id": self.human, "name": "Me"},
                    "onBehalfOf": None,
                }
            ],
        )
        self.assertEqual(
            self.gql(issue_read.Q_READ, i=child["id"])["issue"]["parent"],
            {
                "id": self.task7["id"],
                "identifier": "TASK-7",
                "title": "Session registry",
                "state": {"name": "In Progress"},
            },
        )
        ops.call(self.gql, linear_tracker.M_ARCHIVE, "issueArchive", i=child["id"])
        self.assertEqual(self.gql(issue_read.Q_READ, i="TASK-7")["issue"]["children"]["nodes"], [])

    def test_read_cuts_each_connection_to_its_first_and_more_reads_on(self):
        at = datetime(2026, 10, 9, tzinfo=UTC)
        ids = [
            self.fake.comment("TASK-7", self.human, f"#{n}", at=at + timedelta(minutes=n))
            for n in range(6)
        ]
        page = self.gql(issue_read.Q_READ, i="TASK-7")["issue"]["comments"]
        self.assertEqual([n["id"] for n in page["nodes"]], ids[:0:-1])
        self.assertEqual(page["pageInfo"], {"hasNextPage": True, "endCursor": ids[1]})
        rest = self.gql(issue_read.MORE["comments"], i=self.task7["id"], c=ids[1])
        self.assertEqual([n["id"] for n in rest["issue"]["comments"]["nodes"]], [ids[0]])
        self.assertEqual(
            rest["issue"]["comments"]["pageInfo"], {"hasNextPage": False, "endCursor": ids[0]}
        )
        full = self.gql(issue_read.Q_FULL, i="TASK-7")["issue"]["comments"]
        self.assertEqual(
            ([n["id"] for n in full["nodes"]], full["pageInfo"]["hasNextPage"]), (ids[::-1], False)
        )
        self.assertEqual(
            (
                self.issues.view("TASK-7", full=False)["truncated"],
                len(self.issues.view("TASK-7", full=True)["comments"]),
            ),
            (["comments"], 6),
        )
        with self.assertRaisesRegex(LinearError, f"fake: no cursor {PROJECT['id']}"):
            self.gql(issue_read.MORE["comments"], i="TASK-7", c=PROJECT["id"])
        for name in ("labels", "relations", "inverseRelations", "children"):
            with self.subTest(name):
                self.assertEqual(
                    self.gql(issue_read.MORE[name], i="TASK-7", c=None),
                    {"issue": {name: {"nodes": [], "pageInfo": pages([])}}},
                )

    def test_list_pages_by_cursor_most_recently_updated_first(self):
        eight, nine = self.fake.issue("Eight"), self.fake.issue("Nine")
        self.fake.comment("TASK-8", self.human, "Touched.")
        team = {"team": {"id": {"eq": TEAM}}}
        first = self.gql(issue_list.Q_LIST, f=team, a=False, first=2, after=None)["issues"]
        self.assertEqual(
            first["edges"][0],
            {
                "cursor": eight["id"],
                "node": {
                    "id": eight["id"],
                    "identifier": "TASK-8",
                    "title": "Eight",
                    "url": eight["url"],
                    "state": {"id": STATES["todo"], "name": "Todo"},
                    "assignee": None,
                    "project": None,
                    "parent": None,
                    "labels": {"nodes": [], "pageInfo": {"hasNextPage": False}},
                },
            },
        )
        self.assertEqual([e["cursor"] for e in first["edges"]], [eight["id"], nine["id"]])
        self.assertEqual(first["pageInfo"], {"hasNextPage": True, "endCursor": nine["id"]})
        rest = self.gql(issue_list.Q_LIST, f=team, a=False, first=2, after=nine["id"])["issues"]
        self.assertEqual(
            ([e["node"]["identifier"] for e in rest["edges"]], rest["pageInfo"]),
            (["TASK-7"], {"hasNextPage": False, "endCursor": self.task7["id"]}),
        )
        rows, more, end = self.issues.page(list_query(), 1, eight["id"])
        self.assertEqual(
            ([r[0]["identifier"] for r in rows], more, end), (["TASK-9"], True, nine["id"])
        )

    def test_list_filters(self):
        self.fake.issue(
            "Widget parser",
            delegate=self.engineer,
            assignee=self.human,
            priority=2,
            labels=[TASK_LABELS["build"]],
        )
        self.fake.issue(
            "Other",
            description="Mentions WIDGET here.",
            parent="TASK-7",
            priority=1,
            labels=[CLIENT_LABELS["codex"]],
        )
        self.fake.issue("Mine", state="backlog", delegate=self.manager, assignee=self.engineer)
        gone = self.fake.issue("Archived widget", delegate=self.engineer)
        ops.call(self.gql, linear_tracker.M_ARCHIVE, "issueArchive", i=gone["id"])
        for given, want in (
            ({}, ["TASK-10", "TASK-7", "TASK-8", "TASK-9"]),
            ({"include_archived": True}, ["TASK-10", "TASK-11", "TASK-7", "TASK-8", "TASK-9"]),
            ({"query": "widget"}, ["TASK-8", "TASK-9"]),
            ({"query": "widget", "include_archived": True}, ["TASK-11", "TASK-8", "TASK-9"]),
            ({"state": IssueState.READY}, ["TASK-8", "TASK-9"]),
            ({"state": IssueState.RUNNING}, ["TASK-7"]),
            ({"role": "engineer"}, ["TASK-7", "TASK-8"]),
            ({"role": "me"}, ["TASK-10"]),
            ({"role": "unassigned"}, ["TASK-9"]),
            ({"task": "build"}, ["TASK-8"]),
            ({"client": "codex"}, ["TASK-9"]),
            ({"priority": Priority.HIGH}, ["TASK-8"]),
            ({"priority": Priority.URGENT}, ["TASK-9"]),
            ({"parent": "TASK-7"}, ["TASK-9"]),
            ({"state": IssueState.READY, "role": "engineer"}, ["TASK-8"]),
        ):
            with self.subTest(given):
                self.assertEqual(self.listed(**given), want)

    def test_edit_reads_labels_and_updates_fields_and_labels(self):
        build, codex = TASK_LABELS["build"], CLIENT_LABELS["codex"]
        rec = self.fake.issue("Edit me", description="Old", labels=[build, codex])
        labels = [
            {"id": build, "name": "build", "parent": {"id": TASK_GROUP}},
            {"id": codex, "name": "codex", "parent": {"id": CLIENT_GROUP}},
        ]
        self.assertEqual(
            self.gql(issue_edit.Q_READ, i="TASK-8"),
            {
                "viewer": self.me,
                "issue": {
                    "id": rec["id"],
                    "identifier": "TASK-8",
                    "url": rec["url"],
                    "title": "Edit me",
                    "description": "Old",
                    "priority": 0,
                    "assignee": None,
                    "delegate": None,
                    "project": None,
                    "team": {"id": TEAM, "key": "TASK"},
                    "labels": {"nodes": labels, "pageInfo": pages(labels)},
                },
            },
        )
        change = issue_change(
            "TASK-8",
            title="Edited",
            body="New",
            task="light-build",
            client="claude",
            priority=Priority.HIGH,
        )
        receipt = self.issues.edit(change)
        self.assertEqual(
            (receipt.result, receipt.actor_id, receipt.actor_name),
            ("changed", self.harness, "Harness"),
        )
        self.assertEqual(
            self.fake.requests[-1].variables["in"],
            {
                "title": "Edited",
                "description": "New",
                "priority": 2,
                "addedLabelIds": [TASK_LABELS["light-build"], CLIENT_LABELS["claude"]],
                "removedLabelIds": [build, codex],
            },
        )
        self.assertEqual(
            {k: rec[k] for k in ("title", "description", "priority", "labels")},
            {
                "title": "Edited",
                "description": "New",
                "priority": 2,
                "labels": [TASK_LABELS["light-build"], CLIENT_LABELS["claude"]],
            },
        )
        ops.call(
            self.gql,
            issue_edit.M_EDIT,
            "issueUpdate",
            i="TASK-8",
            **{"in": {"stateId": STATES["done"]}},
        )
        self.assertEqual(
            (rec["state"], rec["history"][-1]["actorId"]), (STATES["done"], self.harness)
        )
        for name, bad, error in (
            ("an unknown input", {"cycleId": TEAM}, "fake: issueUpdate has no input cycleId"),
            ("a label group", {"addedLabelIds": [TASK_GROUP]}, "Entity not found: IssueLabel"),
            (
                "two labels of a group",
                {"addedLabelIds": [build]},
                f"fake: two labels of group {TASK_GROUP}",
            ),
            ("an unknown state", {"stateId": TEAM}, "Entity not found: WorkflowState"),
        ):
            with self.subTest(name), self.assertRaisesRegex(LinearError, error):
                ops.call(self.gql, issue_edit.M_EDIT, "issueUpdate", i="TASK-8", **{"in": bad})
        self.assertEqual(rec["labels"], [TASK_LABELS["light-build"], CLIENT_LABELS["claude"]])

    def test_edit_role_delegates_clears_the_assignee_and_unassigns_only(self):
        rec = self.fake.issue("Unassigned", description="Kept", assignee=self.human)
        for role, want, sent in (
            ("engineer", self.engineer, {"delegateId": self.engineer, "assigneeId": None}),
            ("me", self.manager, {"delegateId": self.manager}),
            ("unassigned", None, {"delegateId": None}),
        ):
            with self.subTest(role=role):
                receipt = self.issues.edit(issue_change(rec["identifier"], role=role))
                self.assertEqual(receipt.result, "changed")
                self.assertEqual(self.fake.requests[-1].variables["in"], sent)
                self.assertEqual(
                    (rec["delegate"], rec["assignee"], rec["title"], rec["description"]),
                    (want, None, "Unassigned", "Kept"),
                )
        again = self.issues.edit(issue_change(rec["identifier"], role="unassigned"))
        self.assertEqual(again.result, "unchanged")

    def test_label_more_reads_after_the_cursor(self):
        build, codex = TASK_LABELS["build"], CLIENT_LABELS["codex"]
        rec = self.fake.issue("Labelled", labels=[build, codex])
        node = {"id": codex, "name": "codex", "parent": {"id": CLIENT_GROUP}}
        self.assertEqual(
            self.gql(issue_label.Q_MORE, i=rec["id"], c=build),
            {"issue": {"labels": {"nodes": [node], "pageInfo": pages([node])}}},
        )

    def test_move_and_comment(self):
        issue = {"id": self.task7["id"], "identifier": "TASK-7", "url": self.task7["url"]}
        self.assertEqual(
            self.gql(issue_write.Q_MOVE, i="TASK-7"),
            {
                "viewer": self.me,
                "issue": {
                    **issue,
                    "archivedAt": None,
                    "state": {"id": STATES["in_progress"]},
                    "team": {"id": TEAM, "key": "TASK"},
                },
            },
        )
        self.assertEqual(
            self.gql(issue_write.Q_COMMENT, i="TASK-7"),
            {"viewer": self.me, "issue": {**issue, "archivedAt": None}},
        )
        made = self.gql(issue_write.M_COMMENT, i=self.task7["id"], b="Raw.")
        self.assertEqual(
            made,
            {
                "commentCreate": {
                    "success": True,
                    "comment": {"id": self.task7["comments"][-1]["id"]},
                }
            },
        )
        self.fake.requests.clear()
        self.assertEqual(self.issues.move("TASK-7", IssueState.DONE).result, "changed")
        receipt = self.issues.comment("TASK-7", "Done.")
        last = self.task7["comments"][-1]
        self.assertEqual(
            (receipt.comment_id, last["body"], last["user"], self.task7["state"]),
            (last["id"], "Done.", self.harness, STATES["done"]),
        )
        self.assertEqual(
            self.ops(),
            [
                "issue_write.Q_MOVE",
                "linear.M_STATE",
                "issue_write.Q_COMMENT",
                "issue_write.M_COMMENT",
            ],
        )

    def test_an_archived_issue_moved_to_todo_is_listed_again(self):
        flt = {"team": {"id": {"eq": TEAM}}, "state": {"id": {"eq": STATES["todo"]}}}

        def todo():
            nodes = self.gql(linear_tracker.q_issues(), f=flt)["issues"]["nodes"]
            return [n["identifier"] for n in nodes]

        with self.fake.lock:
            self.task7["archived"] = True
        self.assertNotIn("TASK-7", todo())
        with self.assertRaisesRegex(IssueError, "TASK-7: archived"):
            self.issues.comment("TASK-7", "Back.")
        self.fake.requests.clear()
        self.assertEqual(self.issues.move("TASK-7", IssueState.READY).result, "changed")
        self.assertEqual(
            self.ops(), ["issue_write.Q_MOVE", "writeback.M_UNARCHIVE", "linear.M_STATE"]
        )
        self.assertFalse(self.task7["archived"])
        self.assertIn("TASK-7", todo())
        self.assertEqual(self.issues.comment("TASK-7", "Back.").result, "changed")

    def test_relation_add_and_remove_both_directions(self):
        eight = self.fake.issue("Eight")
        self.assertEqual(
            self.gql(issue_relation.M_ADD, i=eight["id"], r=self.task7["id"]),
            {"issueRelationCreate": {"success": True}},
        )
        self.assertEqual(self.fake.relations, [("blocks", eight["id"], self.task7["id"])])
        blocks = self.gql(issue_relation.Q_READ["blocks"], i="TASK-8")
        [edge] = blocks["issue"]["relations"]["nodes"]
        self.assertEqual(
            edge, {"id": edge["id"], "type": "blocks", "relatedIssue": {"id": self.task7["id"]}}
        )
        self.assertEqual(blocks["viewer"], self.me)
        self.assertEqual(
            self.gql(issue_relation.Q_READ["blocked_by"], i="TASK-7")["issue"]["inverseRelations"],
            {
                "nodes": [{"id": edge["id"], "type": "blocks", "issue": {"id": eight["id"]}}],
                "pageInfo": pages([edge]),
            },
        )
        self.assertEqual(
            self.gql(issue_relation.Q_MORE["blocks"], i=eight["id"], c=edge["id"]),
            {"issue": {"relations": {"nodes": [], "pageInfo": pages([])}}},
        )
        self.assertEqual(
            len(
                self.gql(issue_relation.Q_MORE["blocked_by"], i="TASK-7", c=None)["issue"][
                    "inverseRelations"
                ]["nodes"]
            ),
            1,
        )
        self.assertEqual(
            self.gql(issue_relation.M_REMOVE, i=edge["id"]),
            {"issueRelationDelete": {"success": True}},
        )
        self.assertEqual(self.fake.relations, [])
        with self.assertRaisesRegex(LinearError, "Entity not found: IssueRelation"):
            self.gql(issue_relation.M_REMOVE, i=edge["id"])
        for op, ident, direction, other, result, left in (
            ("add", "TASK-7", "blocked_by", "TASK-8", "changed", 1),
            ("add", "TASK-8", "blocks", "TASK-7", "unchanged", 1),
            ("remove", "TASK-8", "blocks", "TASK-7", "changed", 0),
            ("remove", "TASK-7", "blocked_by", "TASK-8", "unchanged", 0),
        ):
            with self.subTest(op=op, ident=ident):
                receipt = self.issues.relation(op, ident, direction, other)
                self.assertEqual((receipt.result, len(self.fake.relations)), (result, left))

    def test_archive_is_refused_with_an_unfinished_child_then_takes_it(self):
        child = self.fake.issue("Child", parent="TASK-7")
        plan = self.issues.archive_plan("TASK-7")
        self.assertEqual(
            (plan.outcome, [s.ident for s in plan.unfinished]), ("refused", ["TASK-7", "TASK-8"])
        )
        with self.assertRaises(ArchiveRefused):
            self.issues.archive("TASK-7")
        for ident in ("TASK-8", "TASK-7"):
            self.fake.move(ident, "done", actor=self.human)
        self.assertEqual(self.issues.archive_plan("TASK-7").outcome, "changed")
        self.assertEqual(self.issues.archive("TASK-7").result, "changed")
        self.assertEqual((self.task7["archived"], child["archived"]), (True, True))
        self.assertEqual(self.issues.archive("TASK-7").result, "unchanged")
        self.assertIn("issue_archive.Q_ISSUE", self.ops())


class RefusedKeychain(Case):
    """The fake transport's audit hook: no real `security` process starts under the fake."""

    def refused(self, event, args):
        err = StringIO()
        with (
            redirect_stderr(err),
            self.assertRaisesRegex(RuntimeError, "refused a real Keychain call"),
        ):
            refuse_keychain(event, args)
        self.assertIn("fake_transport.py: refused a real Keychain call", err.getvalue())

    def test_a_security_spawn_is_refused_by_any_route(self):
        argv = ["/usr/bin/security", "find-generic-password", "-s", "svc", "-w"]
        for event, args in (
            ("subprocess.Popen", ("/usr/bin/security", argv, None, None)),
            ("subprocess.Popen", (None, ["security", "find-generic-password"], None, None)),
            (
                "subprocess.Popen",
                (Path("/usr/bin/security"), [Path("/usr/bin/security")], None, None),
            ),
            (
                "subprocess.Popen",
                ("/bin/sh", ["/bin/sh", "-c", "security find-generic-password"], None, None),
            ),
            ("subprocess.Popen", (b"/usr/bin/security", [b"/usr/bin/security"], None, None)),
            ("os.exec", ("/usr/bin/security", argv, None)),
            ("os.posix_spawn", ("/usr/bin/security", argv, None)),
        ):
            with self.subTest(event=event, args=args):
                self.refused(event, args)

    def test_other_processes_and_events_pass(self):
        for event, args in (
            ("subprocess.Popen", ("/bin/echo", ["/bin/echo", "security"], None, None)),
            ("subprocess.Popen", ("/usr/bin/git", ["git", "status"], None, None)),
            ("os.exec", ("/bin/true", ["true"], None)),
            ("open", ("/usr/bin/security", "r", 0)),
        ):
            with self.subTest(event=event, args=args):
                self.assertIsNone(refuse_keychain(event, args))
