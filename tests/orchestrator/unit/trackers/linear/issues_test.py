"""The issue command's Linear backend: list and lookup pages, and each method's Keychain or process
failure as the IssueError naming the manager app's Keychain (a KeychainError keeps its own text).
"""

from ensemble.orchestrator.engine.types import (
    IssueError,
    KeychainError,
    LinearError,
    LookupQuery,
    Resource,
)
from ensemble.orchestrator.trackers.linear.issues import LinearIssues
from tests.orchestrator.case import Case
from tests.orchestrator.unit.issue_fixtures import (
    Gql,
    Routes,
    edges,
    issue_change,
    issue_row,
    list_query,
    names_cfg,
    new_issue,
    team_ref,
)
from tests.utils.process_fixtures import CalledProcessError, TimeoutExpired

TEAMS = LookupQuery(resource=Resource.TEAMS, query=None, team=None, project=None)


def backend(gql):
    return LinearIssues(gql, names_cfg())


def calls(issues):
    """Each method, with arguments that reach Linear before anything else."""
    return {
        "view": lambda: issues.view("TASK-2", full=False),
        "page": lambda: issues.page(list_query(), 20, None),
        "walk": lambda: issues.walk(list_query()),
        "lookup_page": lambda: issues.lookup_page(TEAMS, 20, None),
        "lookup_walk": lambda: issues.lookup_walk(TEAMS),
        "create": lambda: issues.create(new_issue(body="body")),
        "edit": lambda: issues.edit(issue_change("TASK-2", title="t")),
        "move": lambda: issues.move("TASK-2", "done"),
        "comment": lambda: issues.comment("TASK-2", "body"),
        "relation": lambda: issues.relation("add", "TASK-2", "blocks", "TASK-3"),
        "archive_plan": lambda: issues.archive_plan("TASK-2"),
        "archive": lambda: issues.archive("TASK-2"),
        "label": lambda: issues.label("swap", "TASK-2", "bug", "chore"),
    }


class Keychain(Case):
    def test_every_method_names_the_manager_apps_keychain_and_the_error_type(self):
        for error in (
            CalledProcessError(44, ["security"]),
            TimeoutExpired("security", 30),
            FileNotFoundError("security"),
        ):
            for name, call in calls(backend(Gql(None, fail=error))).items():
                with self.subTest(error=type(error).__name__, method=name):
                    with self.assertRaises(IssueError) as cm:
                        call()
                    self.assertEqual(
                        str(cm.exception),
                        f"Linear manager app: Keychain ({type(error).__name__})",
                    )

    def test_a_keychain_error_keeps_its_identity_and_fix(self):
        failure = KeychainError("Linear manager app token expired and renewal failed: x; fix: y")
        for name, call in calls(backend(Gql(None, fail=failure))).items():
            with self.subTest(method=name), self.assertRaises(IssueError) as cm:
                call()
            self.assertEqual(str(cm.exception), str(failure))

    def test_another_linear_failure_passes_unchanged(self):
        failure = LinearError("linear api error: boom")
        for name, call in calls(backend(Gql(None, fail=failure))).items():
            with self.subTest(method=name), self.assertRaises(LinearError) as cm:
                call()
            self.assertIs(cm.exception, failure)


class Pages(Case):
    def test_list_reads_issues_and_lookup_its_resource(self):
        gql = Routes(issues=edges([issue_row("TASK-1")]), teams=edges([team_ref(1, "TASK")]))
        issues = backend(gql)
        rows, more, _ = issues.page(list_query(), 20, None)
        self.assertEqual(([item["identifier"] for item, _, _ in rows], more), (["TASK-1"], False))
        self.assertEqual(
            [item["identifier"] for item, _, _ in issues.walk(list_query())], ["TASK-1"]
        )
        rows, more, _ = issues.lookup_page(TEAMS, 20, None)
        self.assertEqual(([item["key"] for item, _, _ in rows], more), (["TASK"], False))
        self.assertEqual([item["key"] for item, _, _ in issues.lookup_walk(TEAMS)], ["TASK"])
        self.assertEqual((len(gql.sent("issues")), len(gql.sent("teams"))), (2, 2))
