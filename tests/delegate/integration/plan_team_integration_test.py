"""Plan access checks for an owned team dir."""

import json
import os
from unittest import mock

from ensemble.delegate.composer import teams
from ensemble.delegate.composer.base import TEAM
from tests.delegate.integration.cli import owned_team
from tests.delegate.plans import TASKS, TASKS_PARAM, Base


class CustomTeam(Base):
    """A team dir given as the run's team: check_team_dirs against what the run can write."""

    def setUp(self):
        super().setUp()
        self.team = owned_team(self)
        self.tasks = os.path.join(self.team, "tasks")
        self.found = teams.team(TEAM, [self.team])

    def read_unadded(self, role, task, reads, **kw):
        """
        `role` planned for the fake and the codex client: no added dir, `reads` its read dirs.
        """
        home = os.path.join(self.tmp, "codex")
        for client in ("fake", "codex"):
            with (
                self.subTest(role=role, client=client),
                mock.patch.dict(os.environ, {"CODEX_HOME": home}),
            ):
                got, _ = self.planned(role, task, team=self.found, client=client, **kw)
                self.assertEqual((got.session.access.dirs, got.session.access.reads), ((), reads))
        return got

    def test_the_roles_own_tasks_dir_is_a_read_dir_never_added(self):
        got = self.read_unadded("poet", "verse", (self.tasks, TEAM))
        self.assertEqual(TASKS_PARAM.search(got.prompt)[1], self.tasks)

    def test_a_read_dir_holding_it_or_its_tasks_dir_is_read_unadded(self):
        self.read_unadded("poet", "verse", (self.tasks, self.team), keys={"read": [self.team]})
        self.read_unadded(
            "researcher", "research", (TASKS, self.tasks), keys={"read": [self.tasks]}
        )

    def test_one_overlapping_what_the_run_can_write_is_refused(self):
        inside, top = os.path.join(self.team, "w"), os.path.dirname(self.team)
        for kw, what in (
            ({"workdir": inside}, f"the agent dir {os.path.join(inside, 'agent')}"),
            ({"cwd": top}, f"the cwd {top}"),
            ({"keys": {"write": [inside]}}, f"write dir {inside} {inside}"),
            ({"keys": {"write": [self.tasks]}}, f"write dir {self.tasks} {self.tasks}"),
            ({"keys": {"write": [self.team]}}, f"write dir {self.team} {self.team}"),
        ):
            with self.subTest(what=what):
                self.assertEqual(
                    self.refused("poet", "verse", team=self.found, **kw),
                    f"team_dirs: {self.team} overlaps {what}: the agent run can write there",
                )

    def test_one_overlapping_a_dir_codex_can_write_is_refused(self):
        inside, top = os.path.join(self.team, "codex"), os.path.dirname(self.team)
        tmpdir = f"[clients.codex.env]\nTMPDIR = {json.dumps(top)}\n"
        for home, local, what in (
            (inside, "", inside),
            (self.tasks, "", self.tasks),
            (top, "", top),
            (os.path.join(self.tmp, "codex"), tmpdir, top),
        ):
            with self.subTest(home=home, local=local):
                self.local(local)
                with mock.patch.dict(os.environ, {"CODEX_HOME": home}):
                    self.assertEqual(
                        self.refused("poet", "verse", team=self.found, client="codex"),
                        f"team_dirs: {self.team} overlaps a dir codex can write {what}: the "
                        "agent run can write there",
                    )

    def test_one_overlapping_a_dir_the_clients_policy_adds_is_refused(self):
        for client in ("claude", "codex"):
            with self.subTest(client=client):
                self.local(f"[policy.{client}]\nflags = {json.dumps(['--add-dir', self.team])}\n")
                self.assertEqual(
                    self.refused("poet", "verse", team=self.found, client=client),
                    f"team_dirs: {self.team} overlaps a dir {client} can write {self.team}: the "
                    "agent run can write there",
                )
