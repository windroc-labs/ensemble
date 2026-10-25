"""ensemble-orchestrator issue list through main: the configured team's issues, every filter at once,
abstract names as the configured ids, updated first, archived only on request, each item only its
fields.
"""

import json
import os

from tests.orchestrator.unit.board_ids import ACCOUNTS, STATES, TEAM
from tests.orchestrator.unit.issue_fixtures import (
    CLIENT_LABELS,
    MANAGER_KEY,
    OTHER_TEAM,
    ROLE_USERS,
    TASK_LABELS,
    IssueCase,
    Routes,
    edges,
    issue_row,
    label,
    project,
    role_users,
    uid,
    user,
)

FIELDS = ["id", "identifier", "title", "url", "state", "labels", "assignee", "project", "parent_id"]


def of_team(nodes):
    """An issues route answering only the nodes of the filter's team, as Linear would."""

    def answer(variables):
        match = variables["f"]
        scope = (match["and"][0] if "and" in match else match)["team"]["id"]["eq"]
        return edges([n for n in nodes if n["team"]["id"] == scope])

    return answer


class Filters(IssueCase):
    def run_list(self, argv, gql):
        code, out, err = self.run_main(["list", *argv], gql)
        self.assertEqual((code, err), (0, ""), err)
        self.assertLessEqual(len(out), 8192)
        return json.loads(out)

    def test_only_the_configured_team_updated_first_without_archived(self):
        nodes = [issue_row("TASK-1"), issue_row("OPS-1", OTHER_TEAM, title="title TASK-1")]
        gql = Routes(issues=of_team(nodes))
        page = self.run_list([], gql)
        self.assertEqual([i["identifier"] for i in page["items"]], ["TASK-1"])
        query, variables = gql.calls[0]
        self.assertIn("orderBy: updatedAt", query)
        want = {"first": 20, "after": None, "f": {"team": {"id": {"eq": TEAM}}}, "a": False}
        self.assertEqual(variables, {**want, "service": MANAGER_KEY})

    def test_every_filter_holds_at_once(self):
        gql = Routes(
            issueLabels=edges([label(4, "bug")]),
            team={"projects": edges([project(5, "Ensemble")])},
            users=role_users,
            issue={"id": uid(7), "identifier": "TASK-7"},
            issues=edges([]),
        )
        argv = ["--query", "lookup", "--state", "ready", "--role", "engineer", "--task", "build"]
        argv += ["--client", "codex", "--priority", "urgent", "--label", "bug"]
        page = self.run_list([*argv, "--project", "Ensemble", "--parent", "TASK-7"], gql)
        self.assertEqual(
            page, {"items": [], "has_next_page": False, "next_cursor": None, "truncated": []}
        )
        text = {"containsIgnoreCase": "lookup"}
        want = {
            "and": [
                {"team": {"id": {"eq": TEAM}}},
                {"or": [{"title": text}, {"description": text}]},
                {"state": {"id": {"eq": STATES["todo"]}}},
                {"assignee": {"id": {"eq": ROLE_USERS[ACCOUNTS["engineer"]]}}},
                {"labels": {"some": {"id": {"eq": TASK_LABELS["build"]}}}},
                {"labels": {"some": {"id": {"eq": CLIENT_LABELS["codex"]}}}},
                {"priority": {"eq": 1}},
                {"labels": {"some": {"id": {"eq": uid(4)}}}},
                {"project": {"id": {"eq": uid(5)}}},
                {"parent": {"id": {"eq": uid(7)}}},
            ]
        }
        self.assertEqual(gql.sent("issues")[0]["f"], want)

    def test_a_project_name_resolves(self):
        gql = Routes(
            strict=True, team={"projects": edges([project(5, "Ensemble")])}, issues=edges([])
        )
        self.run_list(["--project", "Ensemble"], gql)
        want = [{"team": {"id": {"eq": TEAM}}}, {"project": {"id": {"eq": uid(5)}}}]
        self.assertEqual(gql.sent("issues")[0]["f"], {"and": want})

    def test_each_state_and_priority_by_its_configured_id_and_number(self):
        for argv, want in (
            (["--state", "backlog"], {"state": {"id": {"eq": STATES["backlog"]}}}),
            (["--state", "review"], {"state": {"id": {"eq": STATES["in_review"]}}}),
            (["--state", "canceled"], {"state": {"id": {"eq": STATES["canceled"]}}}),
            (["--priority", "none"], {"priority": {"eq": 0}}),
            (["--priority", "low"], {"priority": {"eq": 4}}),
        ):
            gql = Routes(issues=edges([]))
            with self.subTest(argv=argv):
                self.run_list(argv, gql)
                self.assertEqual(gql.sent("issues")[0]["f"]["and"][1], want)

    def test_role_me_is_the_viewer_and_unassigned_no_assignee(self):
        for spec, want in (
            ("me", {"assignee": {"id": {"eq": uid(6)}}}),
            ("unassigned", {"assignee": {"null": True}}),
        ):
            gql = Routes(viewer=user(6, "Harness"), issues=edges([]))
            with self.subTest(spec=spec):
                self.run_list(["--role", spec], gql)
                self.assertEqual(gql.sent("issues")[0]["f"]["and"][1], want)

    def test_archived_issues_only_when_asked(self):
        gql = Routes(issues=edges([issue_row("TASK-1")]))
        self.run_list(["--include-archived"], gql)
        self.assertTrue(gql.sent("issues")[0]["a"])

    def test_items_hold_only_their_fields(self):
        node = issue_row(
            "TASK-2",
            assignee={"id": uid(6), "name": "Ann"},
            project={"id": uid(5), "name": "Ensemble"},
            parent={"id": uid(9)},
            labels={"nodes": [{"id": uid(4), "name": "bug"}], "pageInfo": {"hasNextPage": True}},
        )
        page = self.run_list([], Routes(issues=edges([node])))
        item = page["items"][0]
        self.assertEqual(list(item), FIELDS)
        self.assertEqual(
            (item["state"], item["labels"], item["assignee"], item["project"], item["parent_id"]),
            (
                {"id": uid(3), "name": "Todo"},
                [{"id": uid(4), "name": "bug"}],
                {"id": uid(6), "name": "Ann"},
                {"id": uid(5), "name": "Ensemble"},
                uid(9),
            ),
        )
        self.assertEqual(page["truncated"], ["items.0.labels"])

    def test_a_page_over_the_limit_keeps_ids_and_continues(self):
        nodes = [issue_row(f"TASK-{n}", title="长" * 300) for n in range(50)]
        gql = Routes(issues=edges(nodes))
        page = self.run_list(["--limit", "50"], gql)
        kept = page["items"]
        self.assertLess(len(kept), 50)
        self.assertEqual(page["next_cursor"], f"cur-{kept[-1]['id']}")
        self.assertTrue(page["has_next_page"])
        self.assertEqual(kept[0]["url"], "https://linear.app/t/issue/TASK-0")

    def test_full_writes_every_matching_issue(self):
        gql = Routes(
            issues=[
                edges([issue_row("TASK-1")], more=True, cursor="c1"),
                edges([issue_row("TASK-2")]),
            ]
        )
        path = os.path.join(self.root, "issues.json")
        code, out, _ = self.run_main(["list", "--full", "--output", path], gql)
        self.assertEqual((code, json.loads(out)), (0, {"output": path}))
        with open(path, encoding="utf-8") as f:
            items = json.load(f)["items"]
        self.assertEqual([i["identifier"] for i in items], ["TASK-1", "TASK-2"])
        self.assertEqual([v["after"] for v in gql.sent("issues")], [None, "c1"])

    def test_full_stays_within_complexity_limit_and_keeps_every_page(self):
        labels = [{"id": uid(n), "name": f"label-{n}"} for n in range(50)]
        nodes = [
            issue_row(
                f"TASK-{n}",
                labels={"nodes": labels, "pageInfo": {"hasNextPage": n == 100}},
            )
            for n in range(121)
        ]

        def answer(variables):
            offset = int(variables["after"] or 0)
            end = offset + variables["first"]
            return edges(nodes[offset:end], more=end < len(nodes), cursor=str(end))

        gql = Routes(strict=True, issues=answer)
        path = os.path.join(self.root, "issues.json")
        code, out, err = self.run_main(["list", "--full", "--output", path], gql)
        self.assertEqual((code, err), (0, ""), err)
        self.assertEqual(json.loads(out), {"output": path})
        with open(path, encoding="utf-8") as f:
            page = json.load(f)
        self.assertEqual([i["identifier"] for i in page["items"]], [n["identifier"] for n in nodes])
        self.assertTrue(all(i["labels"] == labels for i in page["items"]))
        self.assertEqual(page["truncated"], ["items.100.labels"])
        self.assertGreaterEqual(len(gql.sent("issues")), 3)

    def test_full_empty_list_stays_within_complexity_limit(self):
        gql = Routes(strict=True, issues=edges([]))
        path = os.path.join(self.root, "empty.json")
        code, _, err = self.run_main(["list", "--full", "--output", path], gql)
        self.assertEqual((code, err), (0, ""), err)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["items"], [])


class Unresolved(IssueCase):
    def test_an_unresolved_filter_exits_two_before_listing(self):
        cases = (
            (
                ["--label", "Type"],
                Routes(issueLabels=edges([label(4, "Type", is_group=True)])),
                "label group",
            ),
            (["--role", "Ann"], Routes(), "--role Ann: not a role: researcher, pm, engineer, me"),
            (["--role", "pm"], Routes(users={"nodes": []}), ACCOUNTS["pm"]),
            (["--task", "nope"], Routes(), "build, light-build"),
            (["--client", "nope"], Routes(), "codex, claude"),
            (["--parent", "nope"], Routes(), "want an issue identifier"),
        )
        for argv, gql, phrase in cases:
            with self.subTest(argv=argv):
                code, out, err = self.run_main(["list", *argv], gql)
                self.assertEqual((code, out), (2, b""))
                self.assertIn(phrase, err)
                self.assertEqual(gql.sent("issues"), [])
