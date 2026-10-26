"""ensemble-orchestrator issue create: every reference resolved before the one create in the configured
team, only the given fields set, backlog by default, abstract names as the configured ids, then each
blocking relation; a failed relation reported with its whole error.
"""

from ensemble.orchestrator.engine.flow.types import IssueState, Priority
from ensemble.orchestrator.engine.types import (
    Blocking,
    GraphQLErrors,
    IssueError,
    LinearError,
    RelationsFailed,
    Unavailable,
    WriteUnknown,
)
from ensemble.orchestrator.issue.output import receipt_payload
from ensemble.orchestrator.trackers.linear import client, issue_create
from tests.orchestrator.case import Case
from tests.orchestrator.unit.board_ids import MANAGER_USER, STATES, TEAM, role_id
from tests.orchestrator.unit.issue_fixtures import (
    CLIENT_LABELS,
    NEW,
    TASK_LABELS,
    VIEWER,
    by_name,
    create_gql,
    label,
    names_cfg,
    new_issue,
    uid,
)

BODY = 'say "hi", `ls`, $HOME\n中文\n'


def run(fake, cfg=None, **given):
    new = new_issue(**{"title": "Fix login", "body": BODY, "state": IssueState.BACKLOG, **given})
    return issue_create.create(fake, *issue_create.plan(fake, new, cfg or names_cfg()))


def relation(blocker, blocked):
    return {"in": {"type": "blocks", "issueId": blocker, "relatedIssueId": blocked}}


class Create(Case):
    def test_a_project_name_resolves(self):
        fake = create_gql()
        run(fake, project="Ensemble")
        self.assertEqual(fake.writes[0][1]["in"]["projectId"], uid(7))

    def test_every_field_and_both_blocking_directions(self):
        fake = create_gql()
        receipt = run(
            fake,
            labels=("bug", "ops"),
            project="Ensemble",
            milestone="M1",
            parent="TASK-1",
            role="engineer",
            task="build",
            client="codex",
            priority="high",
            state="ready",
            blocks=("TASK-3",),
            blocked_by=("TASK-4",),
        )
        created, *related = fake.writes
        self.assertEqual(
            created[1]["in"],
            {
                "teamId": TEAM,
                "title": "Fix login",
                "description": BODY,
                "stateId": STATES["todo"],
                "labelIds": [TASK_LABELS["build"], CLIENT_LABELS["codex"], uid(4), uid(5)],
                "projectId": uid(7),
                "projectMilestoneId": uid(8),
                "parentId": "id-TASK-1",
                "assigneeId": None,
                "delegateId": role_id("engineer"),
                "priority": 2,
            },
        )
        self.assertEqual(
            [r[1] for r in related],
            [relation("id-TASK-9", "id-TASK-3"), relation("id-TASK-4", "id-TASK-9")],
        )
        self.assertEqual(
            receipt_payload(receipt),
            {"issue": NEW, "operation": "create", "result": "changed", "actor": VIEWER},
        )

    def test_unset_fields_stay_unset_and_the_state_is_backlog(self):
        fake = create_gql()
        run(fake)
        self.assertEqual(
            fake.writes[0][1]["in"],
            {
                "teamId": TEAM,
                "title": "Fix login",
                "description": BODY,
                "stateId": STATES["backlog"],
                "assigneeId": None,
            },
        )
        self.assertEqual(len(fake.writes), 1)

    def test_each_abstract_state_is_its_configured_id(self):
        for state, key in (("running", "in_progress"), ("approved", "handoff"), ("done", "done")):
            fake = create_gql()
            with self.subTest(state=state):
                run(fake, state=IssueState(state))
                self.assertEqual(fake.writes[0][1]["in"]["stateId"], STATES[key])

    def test_each_role_sets_the_delegate_and_every_create_clears_the_assignee(self):
        for role, delegate in (
            ("pm", role_id("pm")),
            ("me", MANAGER_USER),
            ("unassigned", None),
            (None, None),
        ):
            fake = create_gql()
            with self.subTest(role=role):
                run(fake, role=role)
                fields = fake.writes[0][1]["in"]
                self.assertIsNone(fields["assigneeId"])
                self.assertEqual(fields.get("delegateId"), delegate)
                self.assertEqual("delegateId" in fields, delegate is not None)
                self.assertNotIn("query { viewer { id } }", [q for q, _ in fake.calls])

    def test_each_priority_is_its_linear_number(self):
        for name, number in (("none", 0), ("urgent", 1), ("high", 2), ("medium", 3), ("low", 4)):
            fake = create_gql()
            with self.subTest(name=name):
                run(fake, priority=Priority(name))
                self.assertEqual(fake.writes[0][1]["in"]["priority"], number)

    def test_task_and_client_labels_come_first_and_a_label_naming_one_counts_once(self):
        fake = create_gql(issueLabels=by_name(label(0xB2, "Light build"), label(4, "bug")))
        run(fake, task="light-build", client="claude", labels=("bug", "Light build"))
        self.assertEqual(
            fake.writes[0][1]["in"]["labelIds"],
            [TASK_LABELS["light-build"], CLIENT_LABELS["claude"], uid(4)],
        )

    def test_any_unresolved_reference_creates_nothing(self):
        cases = (
            {"labels": ("bug", "nope")},
            {"labels": ("old",)},
            {"project": "Nope"},
            {"milestone": "M1"},
            {"project": "Ensemble", "milestone": "M9"},
            {"parent": "NOPE-1"},
            {"parent": "not an id"},
            {"role": "nobody"},
            {"role": "pm", "labels": ("nope",)},
            {"task": "nope"},
            {"client": "nope"},
            {"blocks": ("TASK-3", "NOPE-2")},
            {"blocked_by": ("NOPE-3",)},
        )
        for given in cases:
            fake = create_gql()
            with self.subTest(given=given), self.assertRaises(IssueError):
                run(fake, **given)
            self.assertEqual(fake.writes, [])

    def test_repeated_labels_and_dependencies_count_once(self):
        fake = create_gql()
        run(fake, labels=("bug", "bug"), blocks=("TASK-3", "TASK-3"), blocked_by=("TASK-4",) * 2)
        created, *related = fake.writes
        self.assertEqual(created[1]["in"]["labelIds"], [uid(4)])
        self.assertEqual(
            [r[1] for r in related],
            [relation("id-TASK-9", "id-TASK-3"), relation("id-TASK-4", "id-TASK-9")],
        )

    def test_one_issue_in_both_directions_is_refused(self):
        fake = create_gql()
        with self.assertRaises(IssueError) as cm:
            run(fake, blocks=("TASK-3",), blocked_by=("TASK-3",))
        self.assertIn("TASK-3", str(cm.exception))
        self.assertIn("--blocks and --blocked-by", str(cm.exception))
        self.assertEqual(fake.writes, [])

    def test_a_failed_relation_carries_what_was_done_and_each_whole_error(self):
        limited = "rate limited " + "x" * 300
        fake = create_gql(
            relations=[
                {"success": True},
                GraphQLErrors([{"message": limited}]),
                Unavailable("issueRelationCreate", reason="timed out"),
            ]
        )
        with self.assertRaises(RelationsFailed) as cm:
            run(fake, blocks=("TASK-3", "TASK-5"), blocked_by=("TASK-4",))
        error = cm.exception
        self.assertEqual(
            receipt_payload(error.receipt),
            {"issue": NEW, "operation": "create", "result": "changed", "actor": VIEWER},
        )
        self.assertEqual(error.done, (Blocking("blocks", "TASK-3"),))
        self.assertEqual(
            [blocking for blocking, _ in error.failed],
            [Blocking("blocks", "TASK-5"), Blocking("blocked_by", "TASK-4")],
        )
        self.assertEqual(error.failed[0][1], limited)
        self.assertIn("timed out", error.failed[1][1])
        self.assertEqual(
            [client.operation(q) for q, _ in fake.writes],
            ["issueCreate"] + ["issueRelationCreate"] * 3,
        )

    def test_a_create_outage_is_result_unknown_and_never_retried(self):
        fake = create_gql(created=Unavailable("issueCreate", reason="timed out"))
        with self.assertRaises(WriteUnknown) as cm:
            run(fake, blocks=("TASK-3",))
        text = str(cm.exception)
        for phrase in ("result unknown", "timed out", "issue list --query='Fix login' before"):
            self.assertIn(phrase, text)
        self.assertNotIn("--team", text)
        self.assertEqual(len(fake.writes), 1)

    def test_the_outage_check_survives_a_dash_title(self):
        fake = create_gql(created=Unavailable("issueCreate", reason="timed out"))
        with self.assertRaises(WriteUnknown) as cm:
            run(fake, title="-x")
        self.assertIn("issue list --query=-x before", str(cm.exception))

    def test_unknown_names_resolve_before_any_read(self):
        for given in ({"task": "nope"}, {"client": "nope"}, {"role": "nobody"}):
            fake = create_gql()
            with self.subTest(given=given), self.assertRaises(IssueError):
                run(fake, labels=("bug",), parent="TASK-1", **given)
            self.assertEqual(fake.calls, [])

    def test_a_client_without_client_labels_is_refused(self):
        fake = create_gql()
        cfg = names_cfg(agent_label_group="", agent_labels={})
        with self.assertRaises(IssueError) as cm:
            run(fake, cfg, client="codex")
        self.assertIn("client labels are not configured", str(cm.exception))
        self.assertEqual(fake.writes, [])

    def test_a_refused_create_fails_and_adds_no_relation(self):
        for created in ({"success": False, "issue": None}, GraphQLErrors([{"message": "boom"}])):
            fake = create_gql(created=created)
            with self.subTest(created=created), self.assertRaises(LinearError) as cm:
                run(fake, blocks=("TASK-3",))
            self.assertNotIsInstance(cm.exception, WriteUnknown)
            self.assertEqual(len(fake.writes), 1)
