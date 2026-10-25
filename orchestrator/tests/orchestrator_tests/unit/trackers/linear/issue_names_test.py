"""issue_names: abstract states, roles, tasks, clients and priorities as the configured Linear ids;
a name the configuration lacks is IssueError naming the configured ones.
"""

from ensemble.orchestrator.engine.flow.types import IssueState
from ensemble.orchestrator.engine.types import IssueError
from ensemble.orchestrator.trackers.linear import issue_names
from orchestrator_tests.case import Case
from orchestrator_tests.unit.board_ids import ACCOUNTS, STATES
from orchestrator_tests.unit.issue_fixtures import (
    CLIENT_LABELS,
    OTHER_TEAM,
    ROLE_USERS,
    TASK_LABELS,
    VIEWER,
    Routes,
    names_cfg,
    role_users,
    write_issue,
)


class StateId(Case):
    def test_each_abstract_state_is_its_configured_key(self):
        keys = {
            IssueState.BACKLOG: "backlog",
            IssueState.READY: "todo",
            IssueState.RUNNING: "in_progress",
            IssueState.REVIEW: "in_review",
            IssueState.APPROVED: "handoff",
            IssueState.DONE: "done",
            IssueState.CANCELED: "canceled",
        }
        self.assertEqual(list(keys), list(IssueState))
        for state, key in keys.items():
            with self.subTest(state=state):
                self.assertEqual(issue_names.state_id(names_cfg(), state), STATES[key])


class Assignee(Case):
    def test_a_role_is_its_configured_app_user(self):
        gql = Routes(users=role_users)
        self.assertEqual(
            issue_names.assignee(gql, names_cfg(), "engineer"), ROLE_USERS[ACCOUNTS["engineer"]]
        )
        self.assertEqual(gql.calls, [])

    def test_me_is_the_viewer_and_unassigned_none(self):
        gql = Routes(viewer=VIEWER)
        self.assertEqual(issue_names.assignee(gql, names_cfg(), "me"), VIEWER["id"])
        self.assertIsNone(issue_names.assignee(gql, names_cfg(), "unassigned"))
        self.assertEqual(len(gql.calls), 1)

    def test_an_unknown_spec_names_every_role_me_and_unassigned(self):
        for spec in ("nobody", "Engineer", "u1@example.com"):
            gql = Routes()
            with self.subTest(spec=spec), self.assertRaises(IssueError) as cm:
                issue_names.assignee(gql, names_cfg(), spec)
            self.assertEqual(
                str(cm.exception),
                f"--role {spec}: not a role: researcher, pm, engineer, me, unassigned",
            )
            self.assertEqual(gql.calls, [])


class Labels(Case):
    def test_a_configured_name_is_its_label(self):
        self.assertEqual(issue_names.task_label(names_cfg(), "build"), TASK_LABELS["build"])
        self.assertEqual(issue_names.client_label(names_cfg(), "codex"), CLIENT_LABELS["codex"])

    def test_an_unknown_name_lists_the_configured_ones(self):
        for find, option, names in (
            (issue_names.task_label, "--task", "build, light-build"),
            (issue_names.client_label, "--client", "codex, claude"),
        ):
            with self.subTest(option=option), self.assertRaises(IssueError) as cm:
                find(names_cfg(), "nope")
            self.assertIn(f"{option} nope", str(cm.exception))
            self.assertIn(names, str(cm.exception))

    def test_no_task_names_configured_says_so(self):
        with self.assertRaises(IssueError) as cm:
            issue_names.task_label(names_cfg(task_labels={}), "build")
        self.assertIn("--task build", str(cm.exception))
        self.assertIn("none configured", str(cm.exception))
        self.assertIn("task_labels", str(cm.exception))

    def test_no_client_group_says_client_labels_are_not_configured(self):
        cfg = names_cfg(agent_label_group="", agent_labels={})
        with self.assertRaises(IssueError) as cm:
            issue_names.client_label(cfg, "codex")
        self.assertIn(
            "client labels are not configured ([orchestrator.linear] agent_label_group)",
            str(cm.exception),
        )


class InTeam(Case):
    def test_an_issue_of_the_configured_team_passes(self):
        self.assertIsNone(issue_names.in_team(names_cfg(), write_issue("TASK-2"), "move"))

    def test_another_team_names_its_key_the_option_and_the_configured_team(self):
        cfg = names_cfg()
        with self.assertRaises(IssueError) as cm:
            issue_names.in_team(cfg, write_issue("OPS-3", team_id=OTHER_TEAM), "--task")
        text = str(cm.exception)
        for phrase in ("OPS-3", "team OPS", "--task", f"linear.team {cfg.team}"):
            self.assertIn(phrase, text)
