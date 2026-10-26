"""ensemble-orchestrator issue edit: one issueUpdate naming only what changes; a task or client
replaces the issue's label of that group, every other label staying; nothing changed writes
nothing.
"""

from ensemble.orchestrator.engine.flow.types import Priority
from ensemble.orchestrator.engine.types import IssueError, Unavailable, WriteUnknown
from ensemble.orchestrator.issue.output import receipt_payload
from ensemble.orchestrator.trackers.linear import issue_edit, issue_label, issue_read, issue_refs
from tests.orchestrator.case import Case
from tests.orchestrator.unit.board_ids import ACCOUNTS, MANAGER_USER, TASK_GROUP, TEAM
from tests.orchestrator.unit.issue_fixtures import (
    CLIENT_GROUP,
    CLIENT_LABELS,
    OTHER_TEAM,
    ROLE_USERS,
    TASK_LABELS,
    VIEWER,
    EditGql,
    issue_change,
    label,
    names_cfg,
    project,
    uid,
)

BUILD, LIGHT = label(0xB1, "build", group=TASK_GROUP), label(0xB2, "light", group=TASK_GROUP)
CODEX, CLAUDE = label(0xC1, "a", group=CLIENT_GROUP), label(0xC2, "b", group=CLIENT_GROUP)
KEEP = label(3, "keep")
CATALOG = (BUILD, LIGHT, CODEX, CLAUDE, KEEP)
BODY = 'say "hi", `ls`, $HOME\n中文\n'
ENSEMBLE, OPS, OPS_TOO = project(4, "Ensemble"), project(5, "Ops"), project(6, "Ops")
PROJECTS = (ENSEMBLE, OPS, project(7, "Elsewhere", team_ids=(OTHER_TEAM,)))


def edit(gql, body=None, cfg=None, **given):
    change = issue_change(**{"ident": "TASK-2", **given, "body": body})
    return issue_edit.edit(gql, cfg or names_cfg(), change)


class Edit(Case):
    def test_each_field_alone_is_the_only_one_written(self):
        for given, body, want in (
            ({"title": "New"}, None, {"title": "New"}),
            ({}, BODY, {"description": BODY}),
            ({"priority": Priority.HIGH}, None, {"priority": 2}),
            ({"priority": Priority.NONE}, None, {"priority": 0}),
        ):
            gql = EditGql(CATALOG, [KEEP["id"]], fields={"priority": 3})
            with self.subTest(given=given, body=body):
                receipt = edit(gql, body, **given)
                self.assertEqual(gql.writes, [(issue_edit.M_EDIT, {"i": "id-TASK-2", "in": want})])
                self.assertEqual(
                    receipt_payload(receipt),
                    {
                        "issue": {
                            "id": "id-TASK-2",
                            "identifier": "TASK-2",
                            "url": "https://linear.app/t/issue/TASK-2",
                        },
                        "operation": "edit",
                        "result": "changed",
                        "actor": VIEWER,
                    },
                )

    def test_every_field_at_once_is_one_write(self):
        gql = EditGql(CATALOG, [BUILD["id"], CODEX["id"], KEEP["id"]])
        edit(gql, BODY, title="New", task="light-build", client="claude", priority=Priority.LOW)
        self.assertEqual(
            gql.writes,
            [
                (
                    issue_edit.M_EDIT,
                    {
                        "i": "id-TASK-2",
                        "in": {
                            "title": "New",
                            "description": BODY,
                            "priority": 4,
                            "addedLabelIds": [TASK_LABELS["light-build"], CLIENT_LABELS["claude"]],
                            "removedLabelIds": [BUILD["id"], CODEX["id"]],
                        },
                    },
                )
            ],
        )

    def test_a_task_or_client_replaces_its_group_label_and_keeps_every_other(self):
        for given, added, gone in (
            ({"task": "light-build"}, TASK_LABELS["light-build"], BUILD["id"]),
            ({"client": "claude"}, CLIENT_LABELS["claude"], CODEX["id"]),
        ):
            gql = EditGql(CATALOG, [BUILD["id"], CODEX["id"], KEEP["id"]])
            with self.subTest(given=given):
                edit(gql, **given)
                (_, variables), *_ = gql.writes
                self.assertEqual(
                    variables["in"], {"addedLabelIds": [added], "removedLabelIds": [gone]}
                )
                kept = {BUILD["id"], CODEX["id"], KEEP["id"], added} - {gone}
                self.assertEqual(set(gql.carried), kept)

    def test_a_task_on_an_issue_without_one_only_adds(self):
        gql = EditGql(CATALOG, [KEEP["id"]])
        edit(gql, task="build")
        self.assertEqual(gql.writes[0][1]["in"], {"addedLabelIds": [TASK_LABELS["build"]]})

    def test_labels_on_later_pages_are_read(self):
        extra = [label(0x1000 + n, f"x{n}") for n in range(issue_read.PAGE)]
        gql = EditGql((*CATALOG, *extra), [n["id"] for n in extra] + [BUILD["id"]])
        edit(gql, task="light-build")
        self.assertIn(
            (issue_label.Q_MORE, {"i": "id-TASK-2", "c": str(issue_read.PAGE)}), gql.calls
        )
        self.assertEqual(gql.writes[0][1]["in"]["removedLabelIds"], [BUILD["id"]])

    def test_every_given_value_already_so_writes_nothing(self):
        gql = EditGql(
            CATALOG,
            [BUILD["id"], CODEX["id"]],
            fields={"title": "Same", "description": BODY, "priority": 2},
        )
        receipt = edit(
            gql, BODY, title="Same", task="build", client="codex", priority=Priority.HIGH
        )
        self.assertEqual((receipt.result, receipt.operation, gql.writes), ("unchanged", "edit", []))
        self.assertEqual(receipt_payload(receipt)["actor"], VIEWER)

    def test_a_role_sets_the_delegate_and_clears_the_assignee(self):
        engineer = ROLE_USERS[ACCOUNTS["engineer"]]
        other = {"id": "u-other"}
        for role, fields, want in (
            ("engineer", {"delegate": other}, {"delegateId": engineer}),
            ("me", {"delegate": other}, {"delegateId": MANAGER_USER}),
            ("engineer", {"delegate": {"id": engineer}, "assignee": other}, {"assigneeId": None}),
            ("me", {"assignee": other}, {"delegateId": MANAGER_USER, "assigneeId": None}),
            ("unassigned", {"delegate": other}, {"delegateId": None}),
            (
                "unassigned",
                {"delegate": other, "assignee": other},
                {"delegateId": None, "assigneeId": None},
            ),
        ):
            gql = EditGql(CATALOG, [KEEP["id"]], fields=fields)
            with self.subTest(role=role, fields=fields):
                self.assertEqual(edit(gql, role=role).result, "changed")
                self.assertEqual(gql.writes, [(issue_edit.M_EDIT, {"i": "id-TASK-2", "in": want})])

    def test_a_role_already_so_writes_nothing(self):
        for role, delegate in (
            ("engineer", {"id": ROLE_USERS[ACCOUNTS["engineer"]]}),
            ("me", {"id": MANAGER_USER}),
            ("unassigned", None),
        ):
            gql = EditGql(CATALOG, [], fields={"delegate": delegate, "assignee": None})
            with self.subTest(role=role):
                self.assertEqual(edit(gql, role=role).result, "unchanged")
                self.assertEqual(gql.writes, [])

    def test_without_a_role_neither_delegate_nor_assignee_is_written(self):
        other = {"id": "u-other"}
        gql = EditGql(CATALOG, [], fields={"delegate": other, "assignee": other})
        self.assertEqual(edit(gql, title="New").result, "changed")
        self.assertEqual(
            gql.writes, [(issue_edit.M_EDIT, {"i": "id-TASK-2", "in": {"title": "New"}})]
        )

    def test_a_project_by_id_or_exact_name_writes_only_the_project(self):
        for value in (ENSEMBLE["id"], "Ensemble"):
            gql = EditGql(CATALOG, [KEEP["id"]], projects=PROJECTS)
            with self.subTest(value=value):
                self.assertEqual(edit(gql, project=value).result, "changed")
                self.assertEqual(
                    gql.writes,
                    [(issue_edit.M_EDIT, {"i": "id-TASK-2", "in": {"projectId": ENSEMBLE["id"]}})],
                )

    def test_a_project_already_set_writes_nothing(self):
        gql = EditGql(CATALOG, [], projects=PROJECTS, fields={"project": {"id": OPS["id"]}})
        self.assertEqual(edit(gql, project="Ops").result, "unchanged")
        self.assertEqual(gql.writes, [])

    def test_a_project_that_does_not_resolve_lists_the_candidates_and_writes_nothing(self):
        lookup = f"ensemble-orchestrator issue lookup projects --team {TEAM}"
        for value, phrase in (
            ("Nope", f"no project of team {TEAM} has that name; candidates: none"),
            ("Ops", f"2 match, name one by id: {OPS['id']} 'Ops', {OPS_TOO['id']} 'Ops'"),
            (uid(7), f"{uid(7)} 'Elsewhere' is not a project of team {TEAM}; candidates: "),
            (uid(9), "no such project"),
        ):
            gql = EditGql(CATALOG, [], projects=(*PROJECTS, OPS_TOO))
            with self.subTest(value=value), self.assertRaises(IssueError) as cm:
                edit(gql, project=value)
            self.assertIn(phrase, str(cm.exception))
            self.assertIn(lookup, str(cm.exception))
            self.assertEqual(gql.writes, [])
        self.assertIn(f"{ENSEMBLE['id']} 'Ensemble'", str(cm.exception))

    def test_a_project_is_resolved_before_the_issue_is_read(self):
        gql = EditGql(CATALOG, [], projects=PROJECTS)
        with self.assertRaises(IssueError):
            edit(gql, project="Nope")
        self.assertNotIn(issue_edit.Q_READ, [query for query, _ in gql.calls])
        self.assertEqual(gql.calls[0][0], issue_refs.projects(TEAM, None)[0])

    def test_an_unconfigured_name_is_refused_before_any_read(self):
        for given in ({"task": "nope"}, {"client": "nope"}, {"role": "nope"}):
            gql = EditGql(CATALOG, [])
            with self.subTest(given=given), self.assertRaises(IssueError):
                edit(gql, **given)
            self.assertEqual(gql.calls, [])

    def test_a_task_client_or_project_on_another_team_writes_nothing(self):
        other = {"team": {"id": OTHER_TEAM, "key": "OPS"}}
        for given in ({"task": "build"}, {"client": "codex", "title": "New"}, {"project": "Ops"}):
            gql = EditGql(CATALOG, [], fields=other, projects=PROJECTS)
            with self.subTest(given=given), self.assertRaises(IssueError) as cm:
                edit(gql, **given)
            self.assertIn("is in team OPS", str(cm.exception))
            self.assertEqual(gql.writes, [])

    def test_title_description_priority_and_role_need_no_team(self):
        for given in ({"title": "New"}, {"role": "pm"}):
            gql = EditGql(CATALOG, [], fields={"team": {"id": OTHER_TEAM, "key": "OPS"}})
            with self.subTest(given=given):
                self.assertEqual(edit(gql, **given).result, "changed")

    def test_no_such_issue_writes_nothing(self):
        gql = EditGql(CATALOG, [])
        with self.assertRaises(IssueError) as cm:
            edit(gql, ident="TASK-3", title="New")
        self.assertIn("no such issue", str(cm.exception))
        self.assertEqual(gql.writes, [])

    def test_a_write_outage_is_result_unknown(self):
        gql = EditGql(CATALOG, [], result=Unavailable("issueUpdate", reason="timed out"))
        with self.assertRaises(WriteUnknown) as cm:
            edit(gql, title="New")
        text = str(cm.exception)
        for phrase in ("edit TASK-2", "result unknown", "timed out", "issue read TASK-2"):
            self.assertIn(phrase, text)
        self.assertEqual(len(gql.writes), 1)
