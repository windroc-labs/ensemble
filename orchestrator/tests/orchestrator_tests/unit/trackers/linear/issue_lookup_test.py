"""ensemble-orchestrator issue lookup through main: each resource's fixed fields and scope, and the
shared page options.
"""

import json
import os

from orchestrator_tests.unit.board_ids import TEAM
from orchestrator_tests.unit.issue_fixtures import (
    OTHER_TEAM,
    IssueCase,
    Routes,
    edges,
    label,
    milestone,
    project,
    state,
    team_ref,
    uid,
    user,
)


class Resources(IssueCase):
    def lookup(self, argv, gql):
        code, out, err = self.run_main(["lookup", *argv], gql)
        self.assertEqual((code, err), (0, ""), err)
        self.assertLessEqual(len(out), 8192)
        return json.loads(out)

    def test_teams_match_name_or_key_and_keep_same_named_ones_apart(self):
        gql = Routes(teams=edges([team_ref(1, "ENG", "Core"), team_ref(2, "OPS", "Core")]))
        page = self.lookup(["teams", "--query", "core"], gql)
        self.assertEqual(
            page["items"],
            [
                {"id": uid(1), "name": "Core", "key": "ENG"},
                {"id": uid(2), "name": "Core", "key": "OPS"},
            ],
        )
        self.assertEqual(
            (page["has_next_page"], page["next_cursor"], page["truncated"]), (False, None, [])
        )
        match = gql.sent("teams")[0]["f"]
        self.assertEqual(match["or"][1], {"key": {"containsIgnoreCase": "core"}})

    def test_states_of_the_configured_team(self):
        gql = Routes(workflowStates=edges([state(3, "Todo"), state(4, "Done", "completed")]))
        page = self.lookup(["states"], gql)
        self.assertEqual(page["items"][1], {"id": uid(4), "name": "Done", "type": "completed"})
        self.assertEqual(gql.sent("workflowStates")[0]["f"], {"team": {"id": {"eq": TEAM}}})

    def test_states_of_a_team_given_by_key(self):
        gql = Routes(teams=edges([team_ref(9, "OPS")]), workflowStates=edges([]))
        page = self.lookup(["states", "--team", "OPS", "--query", "do"], gql)
        self.assertEqual(
            page, {"items": [], "has_next_page": False, "next_cursor": None, "truncated": []}
        )
        want = {"and": [{"team": {"id": {"eq": uid(9)}}}, {"name": {"containsIgnoreCase": "do"}}]}
        self.assertEqual(gql.sent("workflowStates")[0]["f"], want)

    def test_labels_only_ones_an_issue_can_carry(self):
        nodes = [
            label(1, "bug", group=uid(8)),
            label(2, "bug", None),
            label(3, "old", retired="2026-01-01T00:00:00.000Z"),
            label(4, "Type", is_group=True),
        ]
        gql = Routes(issueLabels=edges(nodes))
        page = self.lookup(["labels"], gql)
        self.assertEqual(
            page["items"],
            [
                {"id": uid(1), "name": "bug", "team_id": TEAM, "group_id": uid(8)},
                {"id": uid(2), "name": "bug", "team_id": None, "group_id": None},
            ],
        )
        scope = {"or": [{"team": {"id": {"eq": TEAM}}}, {"team": {"null": True}}]}
        want = {"and": [scope, {"isGroup": {"eq": False}}]}
        self.assertEqual(gql.sent("issueLabels")[0]["f"], want)

    def test_projects_of_the_team_with_their_teams(self):
        gql = Routes(
            team={
                "projects": edges(
                    [
                        project(5, "Ensemble", (TEAM, OTHER_TEAM)),
                        project(6, "Big", (TEAM,), more=True),
                    ]
                )
            }
        )
        page = self.lookup(["projects"], gql)
        self.assertEqual(
            page["items"][0], {"id": uid(5), "name": "Ensemble", "team_ids": [TEAM, OTHER_TEAM]}
        )
        self.assertEqual(page["truncated"], ["items.1.team_ids"])
        self.assertEqual(gql.sent("team")[0]["t"], TEAM)

    def test_milestones_of_the_named_project_only(self):
        gql = Routes(
            team={"projects": edges([project(5, "Ensemble")])},
            project={
                "projectMilestones": edges([milestone(7, "M1", uid(5)), milestone(8, "M1", uid(5))])
            },
        )
        page = self.lookup(["milestones", "--project", "Ensemble"], gql)
        self.assertEqual(
            page["items"],
            [
                {"id": uid(7), "name": "M1", "project_id": uid(5)},
                {"id": uid(8), "name": "M1", "project_id": uid(5)},
            ],
        )
        self.assertEqual(gql.sent("project")[0]["p"], uid(5))

    def test_users_of_the_workspace_or_of_a_team(self):
        gql = Routes(users=edges([user(1, "Ann", active=False)]))
        page = self.lookup(["users", "--query", "ann@"], gql)
        self.assertEqual(
            page["items"],
            [{"id": uid(1), "name": "Ann", "email": "u1@example.com", "active": False}],
        )
        self.assertIn("includeDisabled: true", gql.calls[0][0])
        self.assertEqual(
            gql.sent("users")[0]["f"]["or"][1], {"email": {"containsIgnoreCase": "ann@"}}
        )
        members = Routes(
            teams=edges([team_ref(9, "OPS")]), team={"members": edges([user(2, "Bo")])}
        )
        self.lookup(["users", "--team", "OPS"], members)
        self.assertIn("members(", members.calls[1][0])
        self.assertEqual(members.sent("team")[0]["t"], uid(9))


class Usage(IssueCase):
    def test_scope_options_a_resource_does_not_take_exit_two_before_any_request(self):
        cases = (
            (["milestones"], "needs --project"),
            (["teams", "--team", "ENG"], "takes no --team"),
            (["states", "--project", "X"], "takes no --project"),
        )
        for argv, phrase in cases:
            with self.subTest(argv=argv):
                gql = Routes()
                code, out, err = self.run_main(["lookup", *argv], gql)
                self.assertEqual((code, out, gql.calls), (2, b"", []))
                self.assertIn(phrase, err)

    def test_an_unresolved_team_or_project_exits_two_naming_the_lookup(self):
        gql = Routes(teams=[edges([]), edges([])])
        code, out, err = self.run_main(["lookup", "labels", "--team", "nope"], gql)
        self.assertEqual((code, out), (2, b""))
        self.assertIn("lookup teams --query nope", err)
        gql = Routes(team=[{"projects": edges([])}, {"projects": edges([project(5, "Ensemble")])}])
        code, out, err = self.run_main(["lookup", "milestones", "--project", "Agent"], gql)
        self.assertEqual((code, out), (2, b""))
        self.assertIn(f"lookup projects --team {TEAM} --query Agent", err)
        self.assertIn(uid(5), err)


class Pages(IssueCase):
    def test_default_limit_and_the_second_page_by_cursor(self):
        first = edges([team_ref(1, "A")], more=True, cursor="c1")
        gql = Routes(teams=[first, edges([team_ref(2, "B")])])
        code, out, _ = self.run_main(["lookup", "teams"], gql)
        page = json.loads(out)
        self.assertEqual((code, page["has_next_page"], page["next_cursor"]), (0, True, "c1"))
        code, out, _ = self.run_main(["lookup", "teams", "--cursor", "c1", "--limit", "50"], gql)
        self.assertEqual(json.loads(out)["items"][0]["key"], "B")
        self.assertEqual(
            [(v["first"], v["after"]) for v in gql.sent("teams")], [(20, None), (50, "c1")]
        )

    def test_the_limit_is_one_to_fifty(self):
        for limit in ("0", "51", "-1", "x"):
            with self.subTest(limit=limit), self.assertRaises(SystemExit) as cm:
                self.run_main(["lookup", "teams", "--limit", limit], Routes())
            self.assertEqual(cm.exception.code, 2)

    def test_full_writes_every_page_and_prints_the_path(self):
        gql = Routes(
            team=[
                {"projects": edges([project(5, "A")], more=True, cursor="c1")},
                {"projects": edges([project(6, "B", more=True)])},
            ]
        )
        path = os.path.join(self.root, "projects.json")
        code, out, err = self.run_main(["lookup", "projects", "--full", "--output", path], gql)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), {"output": path})
        with open(path, encoding="utf-8") as f:
            written = json.load(f)
        self.assertEqual([i["name"] for i in written["items"]], ["A", "B"])
        self.assertEqual(written["truncated"], ["items.1.team_ids"])
        self.assertEqual([v["first"] for v in gql.sent("team")], [250, 250])

    def test_full_options_are_checked_before_any_request(self):
        path = os.path.join(self.root, "x.json")
        for argv in (
            ["--full"],
            ["--output", path],
            ["--full", "--output", path, "--cursor", "c1"],
            ["--full", "--output", path, "--limit", "5"],
        ):
            with self.subTest(argv=argv):
                gql = Routes()
                code, out, err = self.run_main(["lookup", "teams", *argv], gql)
                self.assertEqual((code, out, gql.calls), (2, b"", []))
                self.assertTrue(err.startswith("ensemble-orchestrator issue: "), err)
        self.assertFalse(os.path.exists(path))
