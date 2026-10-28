"""A local `team_dirs` of TEAM's files, read by the Composer in a child process and by
delegate/cli.py over the fake `claude` and fake_codex: its tasks dir is read, never added; a read
dir holding the team dir runs unadded, a write dir overlapping it is refused.
"""

import json
import os
import re
import unittest

from ensemble.delegate.composer.prompt import RESUME, RETURN
from ensemble.delegate.engine.types import Layout
from tests.delegate.case import REPO
from tests.delegate.integration.cli import (
    ASKING,
    NATIVE,
    RUN,
    CodexIntegration,
    Integration,
    codex_said,
    owned_team,
)
from tests.delegate.integration.composer import child
from tests.delegate.replies import block, mark
from tests.delegate.verifier import VERIFIER
from tests.utils.case import local_section

CORE_TASKS = os.path.join(REPO, "src", "ensemble", "delegate", "team", "tasks")
CORE_TEAM = os.path.dirname(CORE_TASKS)
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


class ComposerTeamDirs(child.Case):
    """The Composer in a child process over role `writer` of a team dir of TEAM as the local
    `team_dirs`.
    """

    def setUp(self):
        super().setUp()
        self.team = owned_team(self, TEAM)
        self.tasks = os.path.join(self.team, "tasks")
        self.local(
            f"[delegate]\nteam_dirs = {json.dumps([self.team])}\n"
            + local_section(VERIFIER, "delegate")
        )

    def composed(self, **req) -> dict:
        got = self.compose("writer", "note", **req)
        self.assertNotIn("error", got)
        return got

    def test_the_catalog_lists_its_role_after_cores_with_its_sources(self):
        rows = self.child("catalog", self.work)
        self.assertEqual(
            list(rows), ["researcher", "pm", "engineer", "verifier", "dummy-tester", "writer"]
        )
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
                self.assertEqual(
                    (got["intent"]["dirs"], got["intent"]["reads"]),
                    ([], [self.tasks, CORE_TEAM]),
                )
                self.assertEqual(TASKS_PARAM.search(got["prompt"])[1], self.tasks)
                self.assertEqual(got["prompt"].startswith(RESUME), resume)
                self.assertEqual(os.listdir(self.work), [])

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
        self.team = owned_team(self, TEAM)
        self.tasks = os.path.join(self.team, "tasks")
        with open(os.path.join(self.env["HOME"], ".ensemble", "config.local.toml"), "w") as f:
            f.write(
                f"[delegate]\nteam_dirs = {json.dumps([self.team])}\n"
                + local_section(VERIFIER, "delegate")
            )

    def test_a_dry_run(self):
        res = self.drive([], "--task", "note", "--dry-run")
        self.assertEqual(res.returncode, 0, res.stderr)
        plan = json.loads(res.stdout)
        self.assertIn(f"`{self.tasks}`", plan["prompt"])
        self.assertEqual(
            (plan["cwd"], plan["dirs"], plan["reads"]),
            (Layout(self.work).agent, [], [self.tasks, CORE_TEAM]),
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
        self.team = owned_team(self, TEAM)
        self.tasks = os.path.join(self.team, "tasks")
        os.makedirs(os.path.join(self.home, ".ensemble"), exist_ok=True)
        with open(os.path.join(self.home, ".ensemble", "config.local.toml"), "w") as f:
            f.write(
                f"[delegate]\nteam_dirs = {json.dumps([self.team])}\n"
                + local_section(VERIFIER, "delegate")
            )

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


if __name__ == "__main__":
    unittest.main()
