"""Ops boundary behavior."""

from types import SimpleNamespace

from ensemble.orchestrator.engine import types
from ensemble.orchestrator.engine.types import LinearError, Stopped
from ensemble.orchestrator.trackers.linear import linear_tracker, ops
from ensemble.utils import parse_time
from tests.orchestrator.case import Case
from tests.orchestrator.unit.board_ids import (
    ACCOUNTS,
    DUPLICATE,
    FINISHED,
    STATES,
    TASK_GROUP,
    TEAM,
    team_node,
)
from tests.orchestrator.unit.linear_fixtures import (
    CHILD,
    IN_PROGRESS,
    IN_REVIEW,
    ISSUE,
    LABEL1,
    LABEL2,
    OTHER,
    TODO,
    Gql,
    comment_gql,
    node,
)


class RoleIds(Case):
    def test_role_ids_are_configured_without_transport(self):
        roles = {r: SimpleNamespace(user_id=f"app-{r}") for r in ACCOUNTS}
        self.assertEqual(ops.role_ids(roles), {f"app-{r}": r for r in ACCOUNTS})


class TeamAndRoles(Case):
    def test_only_the_team_is_read(self):
        calls = []

        def gql(query, **v):
            calls.append((query, v))
            return {"teams": {"nodes": [team_node()]}}

        for roles in ({}, {"pm": SimpleNamespace(user_id="app-pm")}):
            calls.clear()
            linear = SimpleNamespace(team=TEAM, states=dict(STATES), roles=roles)
            found, ids = ops.team_and_roles(gql, linear)
            self.assertEqual(found, ops.Team(TEAM, "Team", dict(STATES), FINISHED))
            self.assertEqual(ids, {app.user_id: r for r, app in roles.items()})
            self.assertEqual(calls, [(ops.Q_TEAM, {"t": TEAM})])


class TeamCheck(Case):
    def cfg(self):
        return SimpleNamespace(team=TEAM, states=dict(STATES))

    def gql(self, nodes):
        calls = []

        def gql(query, **v):
            calls.append((query, v))
            return {"teams": {"nodes": nodes}}

        gql.calls = calls
        return gql

    def test_ok(self):
        gql = self.gql([team_node()])
        self.assertEqual(ops.team(gql, self.cfg()), ops.Team(TEAM, "Team", dict(STATES), FINISHED))
        self.assertEqual(gql.calls, [(ops.Q_TEAM, {"t": TEAM})])

    def test_finished_is_done_canceled_and_every_canceled_or_duplicate_type(self):
        node = team_node()
        node["states"]["nodes"] += [
            {"id": "s-duplicate", "type": "duplicate"},
            {"id": "s-released", "type": "completed"},
            {"id": "s-backlog", "type": "backlog"},
        ]
        self.assertEqual(
            ops.team(self.gql([node]), self.cfg()).finished,
            {STATES["done"], STATES["canceled"], DUPLICATE, "s-duplicate"},
        )

    def test_team_not_found_or_states_outside_team(self):
        other = [i for k, i in STATES.items() if k not in ("handoff", "done")]
        cases = {
            f"linear.team {TEAM} not found in Linear": [],
            (
                f"[linear.states] not workflow states of team 'Team': handoff {STATES['handoff']}, done {STATES['done']}"
            ): [team_node(other)],
        }
        for message, nodes in cases.items():
            with self.subTest(message):
                with self.assertRaises(LinearError) as cm:
                    ops.team(self.gql(nodes), self.cfg())
                self.assertEqual(str(str(cm.exception)), "orchestrator/config.toml: " + message)


class TaskGroupCheck(Case):
    LABELS = {"light-research": LABEL1, "deep-research": LABEL2}

    def cfg(self, labels=LABELS):
        return SimpleNamespace(task_label_group=TASK_GROUP, task_labels=labels)

    def gql(self, node=None, error=None):
        calls = []

        def gql(query, **v):
            calls.append((query, v))
            if error:
                raise LinearError(error)
            return {"issueLabel": node}

        gql.calls = calls
        return gql

    def group(self, *ids):
        return {"isGroup": True, "children": {"nodes": [{"id": i} for i in ids]}}

    def test_group_ok(self):
        for group, labels in ((self.group(LABEL1, LABEL2, OTHER), self.LABELS), (self.group(), {})):
            with self.subTest(labels=labels):
                gql = self.gql(group)
                self.assertIsNone(ops.task_group(gql, self.cfg(labels)))
                self.assertEqual(gql.calls, [(ops.Q_TASK_GROUP, {"i": TASK_GROUP})])
        self.assertIn(
            "issueLabel(id: $i) { isGroup children(first: 250) { nodes { id } } }",
            ops.Q_TASK_GROUP,
        )

    def test_fails(self):
        error = "linear api error: [{'message': 'Entity not found: IssueLabel'}]"
        not_labels = f"[linear.task_labels] not labels of linear.task_label_group {TASK_GROUP}: "
        cases = [
            (self.gql(self.group(LABEL1)), self.LABELS, not_labels + f"deep-research {LABEL2}"),
            (
                self.gql(self.group(OTHER)),
                {"deep-research": LABEL2, "light-research": LABEL1, "ok": OTHER},
                not_labels + f"deep-research {LABEL2}, light-research {LABEL1}",
            ),
            (
                self.gql(error=error),
                self.LABELS,
                f"linear.task_label_group {TASK_GROUP} not found in Linear: {error}",
            ),
            (
                self.gql(
                    {"isGroup": False, "children": {"nodes": [{"id": LABEL1}, {"id": LABEL2}]}}
                ),
                self.LABELS,
                f"linear.task_label_group {TASK_GROUP} is not a label group",
            ),
        ]
        for gql, labels, message in cases:
            with self.subTest(message):
                with self.assertRaises(LinearError) as cm:
                    ops.task_group(gql, self.cfg(labels))
                self.assertEqual(str(str(cm.exception)), "orchestrator/config.toml: " + message)
                self.assertEqual(len(gql.calls), 1)


class Writes(Case):
    def test_queries_verbatim(self):
        self.assertEqual(
            ops.M_SUBSCRIBE,
            (
                "mutation($i: String!, $e: String!) { issueSubscribe(id: $i, userEmail: $e) { success } }"
            ),
        )
        self.assertEqual(
            ops.M_COMMENT,
            (
                "mutation($i: String!, $b: String!) { commentCreate(input: { issueId: $i, body: $b }) { success } }"
            ),
        )
        self.assertEqual(
            ops.M_COMMENT_ID,
            "mutation($c: String!, $i: String!, $b: String!) { commentCreate(input: { id: $c, "
            "issueId: $i, body: $b }) { success } }",
        )
        self.assertEqual(
            ops.Q_COMMENT,
            "query($c: String!) { comment(id: $c) { id body issue { id } user { isMe } } }",
        )
        self.assertEqual(ops.NOT_FOUND, "Entity not found")
        self.assertEqual(
            ops.M_STATE,
            (
                "mutation($i: String!, $s: String!) { issueUpdate(id: $i, input: { stateId: $s }) { success } }"
            ),
        )
        self.assertEqual(ops.Q_ISSUE_STATE, "query($i: String!) { issue(id: $i) { state { id } } }")
        self.assertEqual(
            ops.HISTORY,
            (
                "history(first: 250, after: $after, orderBy: createdAt) { "
                "nodes { createdAt actorId fromStateId toStateId } pageInfo { hasNextPage endCursor } }"
            ),
        )

    def test_each_query_with_the_history_declares_its_cursor(self):
        found = {
            f"{module.__name__}.{name}": value
            for module in (linear_tracker,)
            for name, value in vars(module).items()
            if isinstance(value, str)
            and "history(first: 250, after: $after" in value
            and value != ops.HISTORY
        }
        self.assertEqual(len(found), 5)
        for name, query in found.items():
            with self.subTest(name=name):
                self.assertIn("$after: String", query)

    def test_call_returns_the_field_and_raises_on_success_false(self):
        gql = Gql()
        self.assertEqual(
            ops.call(gql, ops.M_COMMENT, "commentCreate", i="I", b="x"), {"success": True}
        )
        self.assertEqual(gql.calls, [("comment", {"i": "I", "b": "x"})])
        with self.assertRaises(LinearError) as cm:
            ops.call(
                Gql(fail=lambda _name, _v: False), ops.M_COMMENT, "commentCreate", i="I", b="x"
            )
        self.assertEqual(str(cm.exception), "commentCreate: success: false")

    def test_comment_with_and_without_a_client_id(self):
        gql = Gql()
        ops.comment(gql, "I", "x")
        ops.comment(gql, "I", "y", "c-1")
        self.assertEqual(
            gql.calls,
            [("comment", {"i": "I", "b": "x"}), ("comment_id", {"c": "c-1", "i": "I", "b": "y"})],
        )
        with self.assertRaises(LinearError):
            ops.comment(Gql(fail=lambda _name, _v: False), "I", "y", cid="c-1")

    def test_move(self):
        for state, want, calls in (
            (IN_PROGRESS, None, [("read", {"i": "I"}), ("move", {"i": "I", "s": IN_REVIEW})]),
            (IN_REVIEW, IN_REVIEW, [("read", {"i": "I"})]),
            (TODO, TODO, [("read", {"i": "I"})]),
        ):
            with self.subTest(state=state):
                gql = Gql(state)
                self.assertEqual(ops.move(gql, "I", IN_REVIEW, IN_PROGRESS), want)
                self.assertEqual(gql.calls, calls)

    def test_comment_and_move_order(self):
        gql = Gql()
        self.assertIsNone(
            ops.comment_and_move(
                gql,
                "I",
                "body",
                to=IN_REVIEW,
                frm=IN_PROGRESS,
                emails=("a@x.com", "b@x.com"),
            )
        )
        self.assertEqual(
            gql.calls,
            [
                ("subscribe", {"i": "I", "e": "a@x.com"}),
                ("subscribe", {"i": "I", "e": "b@x.com"}),
                ("read", {"i": "I"}),
                ("move", {"i": "I", "s": IN_REVIEW}),
                ("comment", {"i": "I", "b": "body"}),
            ],
        )
        gql = Gql()
        ops.comment_and_move(gql, "I", "body", to=TODO, frm=IN_PROGRESS)
        self.assertEqual([n for n, _ in gql.calls], ["read", "move", "comment"])

    def test_skipped_or_failed_move_posts_no_comment(self):
        for state in (IN_REVIEW, TODO):
            with self.subTest(state=state):
                gql = Gql(state)
                self.assertEqual(
                    ops.comment_and_move(
                        gql,
                        "I",
                        "body",
                        to=IN_REVIEW,
                        frm=IN_PROGRESS,
                        emails=("a@x.com",),
                    ),
                    state,
                )
                self.assertEqual([n for n, _ in gql.calls], ["subscribe", "read"])
        gql = Gql(
            fail=lambda name, _v: LinearError("linear api error: boom") if name == "move" else None
        )
        with self.assertRaises(LinearError):
            ops.comment_and_move(gql, "I", "body", to=IN_REVIEW, frm=IN_PROGRESS)
        self.assertEqual([n for n, _ in gql.calls], ["read", "move"])

    def test_subscribe_failures_noted_in_the_comment(self):
        def fail(name, v):
            if name == "subscribe":
                return False if v["e"] == "a@x.com" else LinearError("linear api error: boom")

        gql = Gql(fail=fail)
        self.assertIsNone(
            ops.comment_and_move(
                gql,
                "I",
                "body",
                to=IN_REVIEW,
                frm=IN_PROGRESS,
                emails=("a@x.com", "b@x.com"),
            )
        )
        self.assertEqual(
            gql.calls[-1],
            (
                "comment",
                {
                    "i": "I",
                    "b": "body"
                    "\n\nCould not subscribe a@x.com: LinearError: issueSubscribe: success: false"
                    "\n\nCould not subscribe b@x.com: LinearError: linear api error: boom",
                },
            ),
        )
        self.assertEqual(ops.subscribe(Gql(), "I", ("a@x.com",)), "")

    def test_signal_exit_reraised(self):
        gql = Gql(fail=lambda name, _v: Stopped(130) if name == "subscribe" else None)
        with self.assertRaises(Stopped) as cm:
            ops.comment_and_move(
                gql,
                "I",
                "body",
                to=IN_REVIEW,
                frm=IN_PROGRESS,
                emails=("a@x.com", "b@x.com"),
            )
        self.assertEqual(cm.exception.code, 130)
        self.assertEqual(gql.calls, [("subscribe", {"i": "I", "e": "a@x.com"})])


class ClientId(Case):
    def test_promotes_child_id_values(self):
        self.assertEqual(ops.client_id("DR-1", "pm", "2026-09-27T10:00:00.000Z"), CHILD)
        self.assertNotEqual(ops.client_id("DR-1", "pm", "2026-09-27T10:00:01.000Z"), CHILD)
        self.assertNotEqual(ops.client_id("DR-1", "engineer", "2026-09-27T10:00:00.000Z"), CHILD)

    def test_stable_and_shaped_as_uuid_v4(self):
        sid = "0b6f2c1e-6d0a-4c1b-9a51-3f1f6b0e2a7d"
        a = ops.client_id(sid, "session")
        self.assertEqual(a, ops.client_id(sid, "session"))
        self.assertEqual(a, "6aa10f29-00e8-42bb-88f1-e74018389257")
        self.assertNotEqual(a, ops.client_id(sid, "start"))
        self.assertEqual(ops.client_id(sid, "start"), ops.client_id(f"{sid}/start"))
        for cid in (a, CHILD):
            self.assertRegex(
                cid, r"\A[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
            )


class FindComment(Case):
    def test_found(self):
        gql = comment_gql(node("Progress (x): y"))
        self.assertEqual(ops.find_comment(gql, "c-1", ISSUE), "Progress (x): y")
        self.assertEqual(gql.calls, [(ops.Q_COMMENT, {"c": "c-1"})])

    def test_not_found(self):
        for gql in (
            comment_gql(),
            comment_gql(
                error=LinearError("linear api error: [{'message': 'Entity not found: Comment'}]")
            ),
        ):
            with self.subTest(calls=gql):
                self.assertIsNone(ops.find_comment(gql, "c-1", ISSUE))

    def test_other_errors_raise(self):
        for error in (
            LinearError("linear api error: boom"),
            Stopped(130),
            types.Unavailable("comment", status=503),
            RuntimeError("x"),
        ):
            with self.subTest(error=repr(error)), self.assertRaises(type(error)) as cm:
                ops.find_comment(comment_gql(error=error), "c-1", ISSUE)
            self.assertIs(cm.exception, error)

    def test_another_issues_or_authors_comment_is_not_ours(self):
        for found in (
            node(issue="other"),
            node(me=False),
            {**node(), "user": None},
            {**node(), "issue": None},
        ):
            with self.subTest(found=found), self.assertRaises(LinearError) as cm:
                ops.find_comment(comment_gql(found), "c-1", ISSUE)
            self.assertEqual(str(cm.exception), "comment c-1 is not ours")

    def test_not_found_names_only_that_text(self):
        self.assertTrue(
            ops.not_found(
                LinearError("linear api error: [{'message': 'Entity not found: Comment'}]")
            )
        )
        for e in (
            LinearError("linear api error: boom"),
            Stopped(130),
            RuntimeError("Entity not found"),
        ):
            with self.subTest(e=repr(e)):
                self.assertFalse(ops.not_found(e))


class LastMove(Case):
    NODES = [
        {"createdAt": "2026-09-27T10:00:00.000Z", "actorId": "human", "toStateId": TODO},
        {"createdAt": "2026-09-27T12:00:00.000Z", "actorId": "agent", "toStateId": TODO},
        {"createdAt": "2026-09-27T11:00:00.000Z", "actorId": "agent", "toStateId": IN_REVIEW},
        {"createdAt": "2026-09-27T13:00:00.000Z", "actorId": None, "toStateId": IN_PROGRESS},
    ]

    def test_latest_move_into_the_states_kept(self):
        at = parse_time
        for nodes, states, actors, want in (
            (self.NODES, {TODO}, None, at("2026-09-27T12:00:00.000Z")),
            (self.NODES, {TODO, IN_REVIEW}, None, at("2026-09-27T12:00:00.000Z")),
            (self.NODES, {IN_REVIEW}, None, at("2026-09-27T11:00:00.000Z")),
            (self.NODES, {STATES["done"]}, None, None),
            ([], {TODO}, None, None),
            (self.NODES, {TODO}, {"human"}, at("2026-09-27T10:00:00.000Z")),
            (self.NODES, {IN_PROGRESS}, {"human", "agent"}, None),
            (self.NODES, {TODO}, set(), None),
        ):
            with self.subTest(states=states, actors=actors, nodes=len(nodes)):
                keep = None if actors is None else (lambda n, a=actors: n["actorId"] in a)
                self.assertEqual(ops.last_move(nodes, states, keep), want)
