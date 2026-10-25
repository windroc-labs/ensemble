"""A local `team_dirs`, a team dir of TEAM's files. Read by the Composer in a child process
(child.Case): a team dir's role composes with its tasks dir to read, never added; its catalog row; a
read dir holding the team dir composes unadded, a write dir refused. Read by delegate/cli.py as a
subprocess over the fake `claude`: a dry run, --list, a run in headless mode of the team dir's role,
its tasks dir a read rule, never added; over fake_codex: a run and its resume reading the tasks dir
unadded, a read dir holding the team dir run unadded, a write dir or CODEX_HOME overlapping it
refused. Main's tests/integration/core/team_dirs_integration_test.py, the Delegate's part.
"""

import json
import os
import re
import unittest

from ensemble.delegate.composer.prompt import RESUME, RETURN
from ensemble.delegate.engine.types import Layout

from delegate.tests.case import REPO
from delegate.tests.integration.cli import (
    ASKING,
    NATIVE,
    RUN,
    CodexIntegration,
    Integration,
    codex_said,
)
from delegate.tests.integration.composer import child
from delegate.tests.replies import block, mark
from utils.tests.sandbox import owned_directory

# the repo's gitignored work/
TEAMS = os.path.join(REPO, "work")
CORE_TASKS = os.path.join(REPO, "delegate", "src", "ensemble", "delegate", "team", "tasks")
MARK = "[ensemble-progress:start] the topic"
NOTE = (
    f"# Note\n\n## Steps\n\n1. **Report progress:**\n   {MARK}\n2. **Write.** Write it.\n\n"
    "## Resume\n\nRedo step 2.\n"
)
WRITER = (
    "# Writer\n\nYou write notes.\n\n## Tasks\n\n"
    "Pick the task below that fits the input; unsure → `note` (`<tasks>/note.md`).\n\n"
    "- `note` (`<tasks>/note.md`): a short note; always; light.\n"
)
TEAM = {"roles/writer.md": WRITER, "tasks/note.md": NOTE}
TASKS_PARAM = re.compile(r"\n- `<tasks>`: `([^`]+)`\n")


def make_team(case: unittest.TestCase) -> str:
    """A team dir of TEAM's files under TEAMS, removed after `case`'s test."""
    team = owned_directory(case, TEAMS, prefix="team-")
    os.chmod(team, 0o755)
    for rel, text in TEAM.items():
        path = os.path.join(team, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.chmod(os.path.dirname(path), 0o755)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(path, 0o644)
    return team


class ComposerTeamDirs(child.Case):
    """The Composer in a child process over role `writer` of a team dir of TEAM as the local
    `team_dirs`.
    """

    def setUp(self):
        super().setUp()
        self.team = make_team(self)
        self.tasks = os.path.join(self.team, "tasks")
        self.local(f"[delegate]\nteam_dirs = {json.dumps([self.team])}\n")

    def composed(self, **req) -> dict:
        got = self.compose("writer", "note", **req)
        self.assertNotIn("error", got)
        return got

    def test_a_team_dirs_role_composes_with_its_tasks_dir(self):
        got = self.composed()
        self.assertEqual(TASKS_PARAM.search(got["prompt"])[1], self.tasks)
        self.assertEqual((got["intent"]["dirs"], got["intent"]["reads"]), ([], [self.tasks]))
        self.assertEqual(os.listdir(self.work), [])

    def test_the_catalog_lists_its_role_after_cores_with_its_sources(self):
        rows = self.child("catalog", self.work)
        self.assertEqual(list(rows), ["researcher", "pm", "engineer", "dummy-tester", "writer"])
        note = ["a short note; always; light.", os.path.join(self.tasks, "note.md")]
        self.assertEqual(
            rows["writer"], ["local", os.path.join(self.team, "roles", "writer.md"), {"note": note}]
        )
        self.assertEqual({row[1] for role, row in rows.items() if role != "writer"}, {"built-in"})
        self.assertEqual(os.listdir(self.work), [])

    def test_the_prompt_leads_to_its_task_file_in_its_tasks_dir(self):
        got = self.composed()
        prompt = got["prompt"]
        self.assertIn("**Your task**: `note` (`<tasks>/note.md`).", prompt)
        tasks = TASKS_PARAM.search(prompt)[1]
        with open(os.path.join(tasks, "note.md"), encoding="utf-8") as f:
            self.assertIn(f"\n   {MARK}\n", f.read())
        self.assertIn(tasks, got["intent"]["reads"])
        self.assertIn(f"\n{RETURN}\n", prompt)
        self.assertFalse(os.path.exists(os.path.join(self.work, "tasks")))

    def test_a_run_and_its_resume_read_its_tasks_dir_unadded(self):
        for resume in (False, True):
            with self.subTest(resume=resume):
                got = self.composed(resume=resume)
                dirs, reads = got["intent"]["dirs"], got["intent"]["reads"]
                self.assertIn(self.tasks, map(os.path.realpath, reads))
                self.assertNotIn(self.tasks, map(os.path.realpath, dirs))
                self.assertNotIn(CORE_TASKS, dirs + reads)
                self.assertIn(f"`{self.tasks}`", got["prompt"])
                self.assertEqual(got["prompt"].startswith(RESUME), resume)
                self.assertFalse(os.path.exists(os.path.join(self.work, "tasks")))

    def test_a_read_dir_holding_the_team_dir_composes_unadded(self):
        self.local(f"[delegate.roles.writer]\nread = [{json.dumps(self.team)}]\n", "a")
        got = self.composed()
        self.assertEqual(
            (got["intent"]["dirs"], got["intent"]["reads"]), ([], [self.tasks, self.team])
        )

    def test_a_write_dir_holding_the_team_dir_is_refused(self):
        self.local(f"[delegate.roles.writer]\nwrite = [{json.dumps(self.team)}]\n", "a")
        got = self.compose("writer", "note")
        self.assertEqual(
            got,
            {
                ("error"): (
                    f"team_dirs: {self.team} overlaps a dir the run adds {self.team}: the agent "
                    "run can write there"
                )
            },
        )


DONE = {"status": "done", "title": "note", "summary": "A note.", "deliverable": "A note.\n"}


def added(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]


class TeamDirs(Integration):
    """Role `writer` of a team dir (TEAM) as the local config's `team_dirs`."""

    ROLE = "writer"

    def setUp(self):
        super().setUp()
        self.team = make_team(self)
        self.tasks = os.path.join(self.team, "tasks")
        with open(os.path.join(self.env["HOME"], ".ensemble", "config.local.toml"), "w") as f:
            f.write(f"[delegate]\nteam_dirs = {json.dumps([self.team])}\n")

    def test_a_dry_run(self):
        res = self.drive([], "--task", "note", "--dry-run")
        self.assertEqual(res.returncode, 0, res.stderr)
        plan = json.loads(res.stdout)
        self.assertIn(f"`{self.tasks}`", plan["prompt"])
        self.assertEqual(
            (plan["cwd"], plan["dirs"], plan["reads"]),
            (Layout(self.work).agent, [], [self.tasks]),
        )
        self.assertEqual(os.listdir(self.work), [])
        self.assertEqual(self.calls(), [])

    def test_list(self):
        res = self.drive([], argv=[*RUN, "--list"])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(
            f"\nwriter  local  {os.path.join(self.team, 'roles', 'writer.md')}\n"
            f"  note: a short note; always; light.  [{os.path.join(self.tasks, 'note.md')}]\n",
            res.stdout,
        )

    def test_a_run_in_headless_mode_reads_its_task_from_its_tasks_dir(self):
        res = self.drive([mark("start", "noting"), block(DONE)], "--task", "note")
        self.assertEqual(res.returncode, 0, res.stderr)
        for line in ("Progress (start): noting", "delegate: status done"):
            self.assertIn(line, res.stderr)
        (call,) = self.calls()
        self.assertIn(f"`{self.tasks}`", call["argv"][-1])
        self.assertIn(f"Read(/{self.tasks}/**)", call["argv"])
        self.assertEqual((call["cwd"], added(call["argv"])), (Layout(self.work).agent, []))
        self.assertFalse(os.path.exists(os.path.join(self.work, "tasks")))
        self.assertEqual(self.read(self.out), DONE["deliverable"])


class CodexTeamDirs(CodexIntegration):
    """--client codex over fake_codex, role `writer` of a team dir (TEAM) as the local config's
    `team_dirs`.
    """

    ROLE, TASK = "writer", "note"

    def setUp(self):
        super().setUp()
        self.team = make_team(self)
        self.tasks = os.path.join(self.team, "tasks")
        os.makedirs(os.path.join(self.home, ".ensemble"), exist_ok=True)
        self.toml = os.path.join(self.home, ".ensemble", "config.local.toml")
        with open(self.toml, "w") as f:
            f.write(f"[delegate]\nteam_dirs = {json.dumps([self.team])}\n")

    def role(self, key: str) -> None:
        with open(self.toml, "a") as f:
            f.write(f"[delegate.roles.writer]\n{key} = {json.dumps([self.team])}\n")

    def test_a_codex_run_and_its_resume_read_its_tasks_dir_unadded(self):
        for resume, resume_id in ((False, None), (True, NATIVE)):
            with self.subTest(resume=resume):
                res = (
                    self.answer(codex_said(block(DONE)))
                    if resume
                    else self.drive(codex_said(mark("start", "noting"), block(ASKING)))
                )
                self.assertEqual(res.returncode, 0, res.stderr)
                call = self.calls()[-1]
                self.assertEqual(call["resume_id"], resume_id)
                dirs = added(call["argv"])
                self.assertNotIn(self.tasks, map(os.path.realpath, dirs))
                self.assertNotIn(CORE_TASKS, dirs)
                self.assertIn(f"`{self.tasks}`", call["prompt"])
                self.assertFalse(os.path.exists(os.path.join(self.work, "tasks")))
                if resume:
                    self.assertEqual(self.read(self.out), DONE["deliverable"])

    def test_a_read_dir_holding_the_team_dir_runs_unadded(self):
        self.role("read")
        res = self.drive(codex_said(block(DONE)))
        self.assertEqual(res.returncode, 0, res.stderr)
        (call,) = self.calls()
        self.assertFalse({self.team, self.tasks} & set(map(os.path.realpath, added(call["argv"]))))

    def test_a_write_dir_holding_the_team_dir_is_refused(self):
        self.role("write")
        res = self.drive([])
        self.assertEqual((res.returncode, res.stdout), (2, ""), res.stderr)
        self.assertIn(
            f"delegate: team_dirs: {self.team} overlaps write dir {self.team} {self.team}: "
            "the agent run can write there\n",
            res.stderr,
        )
        self.assertEqual(self.calls(), [])
        self.assertEqual(os.listdir(self.work), [])

    def test_one_overlapping_codex_home_exits_2_before_codex_starts(self):
        for home in (os.path.join(self.team, "codex"), os.path.dirname(self.team)):
            with self.subTest(home=home):
                self.env["CODEX_HOME"] = home
                res = self.drive([])
                self.assertEqual((res.returncode, res.stdout), (2, ""), res.stderr)
                self.assertIn(
                    f"delegate: team_dirs: {self.team} overlaps a dir codex can write {home}: "
                    "the agent run can write there\n",
                    res.stderr,
                )
                self.assertEqual(self.calls(), [])
                self.assertEqual(os.listdir(self.work), [])


if __name__ == "__main__":
    unittest.main()
