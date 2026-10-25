"""issue_refs: a value resolves to the one entity it names in scope, else IssueError with the
candidates and the lookup command.
"""

from ensemble.orchestrator.engine.types import GraphQLErrors, IssueError, LinearError
from ensemble.orchestrator.trackers.linear import issue_page, issue_refs
from orchestrator_tests.case import Case
from orchestrator_tests.unit.board_ids import TEAM
from orchestrator_tests.unit.issue_fixtures import (
    LIMIT,
    OTHER_TEAM,
    Routes,
    complexity,
    edges,
    label,
    milestone,
    project,
    team_ref,
    uid,
)

MISSING = GraphQLErrors([{"message": "Entity not found: Team"}])


class Team(Case):
    def test_a_key_or_exact_name_matches_one_team(self):
        gql = Routes(teams=edges([team_ref(1, "TASK")]))
        self.assertEqual(issue_refs.team(gql, "task")["key"], "TASK")
        want = {"or": [{"name": {"eq": "task"}}, {"key": {"eqIgnoreCase": "task"}}]}
        self.assertEqual(gql.sent("teams")[0]["f"], want)

    def test_an_id_is_read_by_id(self):
        gql = Routes(team=team_ref(7, "ENG"))
        self.assertEqual(issue_refs.team(gql, uid(7))["key"], "ENG")
        self.assertEqual(gql.sent("team"), [{"id": uid(7)}])

    def test_an_unknown_id_names_candidates_and_the_lookup(self):
        gql = Routes(team=MISSING, teams=edges([team_ref(1, "ENG")]))
        with self.assertRaises(IssueError) as cm:
            issue_refs.team(gql, uid(7))
        text = str(cm.exception)
        self.assertIn(f"no such team; candidates: {uid(1)} ENG 'team ENG';", text)
        self.assertIn("ensemble-orchestrator issue lookup teams", text)
        self.assertNotIn("--query", text)
        near = gql.sent("teams")[0]
        self.assertEqual((near["first"], near["f"]), (issue_refs.CANDIDATES, None))

    def test_no_candidate_but_more_pages_says_so(self):
        # every candidate on the page was skipped, yet more pages exist
        gql = Routes(issueLabels=[edges([]), edges([label(7, "x", retired="2026")], more=True)])
        with self.assertRaises(IssueError) as cm:
            issue_refs.label(gql, TEAM, "x")
        self.assertIn("candidates: none, …;", str(cm.exception))

    def test_other_failures_are_not_a_missing_entity(self):
        with self.assertRaises(LinearError):
            issue_refs.team(Routes(team=GraphQLErrors([{"message": "rate limited"}])), uid(7))

    def test_several_matches_are_rejected_with_every_candidate(self):
        # the second match is on the next page: the first is never taken alone
        gql = Routes(
            teams=[
                edges([team_ref(1, "ENG", "Core")], more=True, cursor="c1"),
                edges([team_ref(2, "CORE", "Platform")]),
            ]
        )
        with self.assertRaises(IssueError) as cm:
            issue_refs.team(gql, "core")
        text = str(cm.exception)
        for found in (uid(1), uid(2), "2 match", "lookup teams --query core"):
            self.assertIn(found, text)

    def test_no_match_lists_near_candidates_and_the_lookup(self):
        gql = Routes(teams=[edges([]), edges([team_ref(3, "COR", "Corvid")], more=True)])
        with self.assertRaises(IssueError) as cm:
            issue_refs.team(gql, "Cor eng")
        text = str(cm.exception)
        self.assertIn(f"candidates: {uid(3)} COR 'Corvid', …", text)
        self.assertIn("lookup teams --query 'Cor eng'", text)
        near = gql.sent("teams")[1]
        self.assertEqual(near["first"], issue_refs.CANDIDATES)
        self.assertEqual(near["f"]["or"][1], {"key": {"containsIgnoreCase": "Cor eng"}})

    def test_no_candidates_says_none(self):
        with self.assertRaises(IssueError) as cm:
            issue_refs.team(Routes(teams=edges([])), "zzz")
        self.assertIn("candidates: none", str(cm.exception))


class Project(Case):
    def test_an_exact_name_matches_within_the_team(self):
        gql = Routes(team={"projects": edges([project(4, "Ensemble")])})
        self.assertEqual(issue_refs.project(gql, TEAM, "Ensemble")["id"], uid(4))
        sent = gql.sent("team")[0]
        self.assertEqual((sent["t"], sent["f"]), (TEAM, {"name": {"eq": "Ensemble"}}))

    def test_an_id_must_belong_to_the_team(self):
        gql = Routes(project=project(4, "Ensemble"))
        self.assertEqual(issue_refs.project(gql, TEAM, uid(4))["id"], uid(4))
        self.assertEqual(gql.sent("project"), [{"id": uid(4), "t": TEAM}])
        outside = Routes(
            project=project(5, "Elsewhere", team_ids=()),
            team={"projects": edges([project(4, "Ensemble")])},
        )
        with self.assertRaises(IssueError) as cm:
            issue_refs.project(outside, OTHER_TEAM, uid(5))
        text = str(cm.exception)
        self.assertIn(f"is not a project of team {OTHER_TEAM}", text)
        self.assertIn(f"lookup projects --team {OTHER_TEAM}", text)

    def test_a_name_is_found_within_linears_complexity_limit(self):
        gql = Routes(strict=True, team={"projects": edges([project(4, "ensemble-test")])})
        self.assertEqual(issue_refs.project(gql, TEAM, "ensemble-test")["id"], uid(4))
        query, variables = gql.calls[0]
        self.assertEqual(variables["first"], issue_page.PAGE)
        self.assertLessEqual(complexity(query, variables), LIMIT)

    def test_same_named_projects_are_rejected(self):
        gql = Routes(team={"projects": edges([project(4, "Ops"), project(5, "Ops")])})
        with self.assertRaises(IssueError) as cm:
            issue_refs.project(gql, TEAM, "Ops")
        self.assertIn(f"{uid(4)} 'Ops', {uid(5)} 'Ops'", str(cm.exception))


class Complexity(Case):
    """The fixtures' score matches Linear's documented example: 66 points, 14 with first: 10."""

    def test_linears_example(self):
        fields = "{ nodes { id title createdAt } } } }"
        self.assertEqual(complexity("query { viewer { createdIssues " + fields, {}), 66)
        query = "query($first: Int!) { viewer { createdIssues(first: $first) " + fields
        self.assertEqual(complexity(query, {"first": 10}), 14)

    def test_a_strict_routes_refuses_a_query_over_the_limit(self):
        query = "query($first: Int!) { teams(first: $first) { nodes { id } } }"
        gql = Routes(strict=True, teams={"nodes": []})
        self.assertEqual(gql(query, first=LIMIT // 2), {"teams": {"nodes": []}})
        with self.assertRaisesRegex(LinearError, "^linear api error: teams: HTTP 400$"):
            gql(query, first=LIMIT)


class Filters(Case):
    def test_every_skips_empty_filters(self):
        a, b = {"name": {"eq": "a"}}, {"key": {"eq": "b"}}
        self.assertIsNone(issue_refs.every(None, None))
        self.assertEqual(issue_refs.every(a, None), a)
        self.assertEqual(issue_refs.every(a, b), {"and": [a, b]})

    def test_text_filters(self):
        self.assertIsNone(issue_refs.named(None))
        self.assertIsNone(issue_refs.team_text(""))
        self.assertIsNone(issue_refs.user_text(None))
        self.assertEqual(
            issue_refs.user_text("ann"),
            {
                "or": [
                    {"name": {"containsIgnoreCase": "ann"}},
                    {"email": {"containsIgnoreCase": "ann"}},
                ]
            },
        )


class Label(Case):
    def test_a_team_or_workspace_label(self):
        for node in (label(1, "bug"), label(2, "bug", None)):
            with self.subTest(team=node["team"]):
                self.assertEqual(
                    issue_refs.label(Routes(issueLabels=edges([node])), TEAM, "bug"), node
                )
        gql = Routes(issueLabel=label(2, "bug", None))
        self.assertEqual(issue_refs.label(gql, TEAM, uid(2))["id"], uid(2))

    def test_groups_and_retired_labels_are_refused(self):
        for node, phrase in (
            (label(4, "Type", is_group=True), "is a label group"),
            (label(5, "old", retired="2026-01-01T00:00:00.000Z"), "is retired"),
        ):
            for gql in (Routes(issueLabels=edges([node])), Routes(issueLabel=node)):
                value = node["name"] if "issueLabels" in gql.routes else node["id"]
                with self.subTest(phrase=phrase, value=value), self.assertRaises(IssueError) as cm:
                    issue_refs.label(gql, TEAM, value)
                self.assertIn(phrase, str(cm.exception))
                self.assertIn(f"lookup labels --team {TEAM}", str(cm.exception))

    def test_retired_true_takes_a_retired_label_never_a_group(self):
        old = label(5, "old", retired="2026-01-01T00:00:00.000Z")
        for gql, value in (
            (Routes(issueLabels=edges([old])), "old"),
            (Routes(issueLabel=old), uid(5)),
        ):
            with self.subTest(value=value):
                self.assertEqual(issue_refs.label(gql, TEAM, value, retired=True), old)
        group = label(4, "Type", is_group=True)
        with self.assertRaises(IssueError) as cm:
            issue_refs.label(Routes(issueLabel=group), TEAM, uid(4), retired=True)
        self.assertIn("is a label group", str(cm.exception))

    def test_only_settable_same_named_labels_count(self):
        nodes = [label(1, "bug", None, retired="2026-01-01T00:00:00.000Z"), label(2, "bug")]
        self.assertEqual(
            issue_refs.label(Routes(issueLabels=edges(nodes)), TEAM, "bug")["id"], uid(2)
        )

    def test_a_label_of_another_team_is_refused(self):
        gql = Routes(issueLabel=label(6, "bug", OTHER_TEAM), issueLabels=edges([label(1, "bug")]))
        with self.assertRaises(IssueError):
            issue_refs.label(gql, TEAM, uid(6))

    def test_candidates_leave_out_retired_labels(self):
        near = edges([label(7, "bugfix", retired="2026-01-01T00:00:00.000Z"), label(8, "bugs")])
        gql = Routes(issueLabels=[edges([]), near])
        with self.assertRaises(IssueError) as cm:
            issue_refs.label(gql, TEAM, "bug")
        self.assertIn(f"candidates: {uid(8)} 'bugs';", str(cm.exception))
        self.assertIn({"isGroup": {"eq": False}}, gql.sent("issueLabels")[1]["f"]["and"][1]["and"])


class Milestone(Case):
    def test_needs_a_project_and_stays_in_it(self):
        with self.assertRaises(IssueError) as cm:
            issue_refs.milestone(Routes(), None, "M1")
        self.assertIn("needs its --project", str(cm.exception))
        gql = Routes(project={"projectMilestones": edges([milestone(7, "M1", uid(5))])})
        self.assertEqual(issue_refs.milestone(gql, uid(5), "M1")["id"], uid(7))
        self.assertEqual(gql.sent("project")[0]["p"], uid(5))
        other = Routes(
            projectMilestone=milestone(7, "M1", uid(6)),
            project={"projectMilestones": edges([milestone(8, "M1", uid(5))])},
        )
        with self.assertRaises(IssueError) as cm:
            issue_refs.milestone(other, uid(5), uid(7))
        self.assertIn(f"lookup milestones --project {uid(5)}", str(cm.exception))


class Parent(Case):
    def test_an_identifier_or_id(self):
        gql = Routes(issue={"id": uid(9), "identifier": "TASK-9"})
        self.assertEqual(issue_refs.issue(gql, "TASK-9")["id"], uid(9))
        self.assertEqual(gql.sent("issue"), [{"id": "TASK-9"}])

    def test_a_malformed_or_missing_issue_is_refused(self):
        with self.assertRaises(IssueError):
            issue_refs.issue(Routes(), "task 9")
        missing = Routes(issue=GraphQLErrors([{"message": "Entity not found: Issue"}]))
        with self.assertRaises(IssueError) as cm:
            issue_refs.issue(missing, "TASK-9")
        self.assertIn("no such issue", str(cm.exception))
