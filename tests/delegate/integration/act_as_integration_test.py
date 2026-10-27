"""The approved skill command in an isolated Python environment."""

import os
import subprocess

from ensemble.delegate.cli import listing
from ensemble.delegate.composer import Composer, RunParams
from ensemble.delegate.engine.types import Layout
from tests.delegate.case import REPO, VERIFIER, Case
from tests.delegate.skills.act_as import argv, command
from tests.utils.case import local_section
from tests.utils.sandbox import clean_env, python_path

ROLES = ["researcher", "pm", "engineer", "verifier", "dummy-tester"]
BIN = os.path.join(REPO, "bin")  # the plugin's bin/, on a Claude Code session's PATH


class ActAs(Case):
    def test_step_1s_command_lists_the_team(self):
        with open(os.path.join(self.ensemble, "config.local.toml"), "w", encoding="utf-8") as f:
            f.write(local_section(VERIFIER, "delegate"))
        env = clean_env()
        env["PATH"] = os.pathsep.join((BIN, python_path(os.path.join(self.home, "bin"))))
        res = subprocess.run(
            argv(command(1)),
            cwd=self.home,
            env={**env, "HOME": self.home},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            timeout=60,
        )
        want = listing(Composer())
        self.assertEqual((res.returncode, res.stdout), (0, want), res.stderr)
        self.assertEqual([r.split()[0] for r in want.splitlines() if r[:1] != " "], ROLES)

    def test_step_3_delivers_the_instruction_file(self):
        workdir = os.path.join(self.home, "work")
        out = os.path.join(Layout(workdir).agent, "prompt.md")
        run = (
            command(3)
            .replace("<role>", "dummy-tester")
            .replace("[--task <task>]", "--task echo")
            .replace("<workdir>", workdir)
            .replace("<Input>", "Hello.")
        )
        env = clean_env()
        env["PATH"] = os.pathsep.join((BIN, python_path(os.path.join(self.home, "bin"))))
        result = subprocess.run(
            argv(run),
            cwd=self.home,
            env={**env, "HOME": self.home},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            timeout=60,
        )
        self.assertEqual(
            (result.returncode, result.stdout, result.stderr),
            (0, "", f"delegate: instruction delivered: {out}\n"),
        )
        composition = Composer()
        expected = composition.render(
            composition.resolve("dummy-tester", "echo"),
            RunParams(input="Hello.", out=out, workdir=Layout(workdir).agent),
        )
        with open(out, "rb") as stream:
            self.assertEqual(stream.read(), expected.encode("utf-8"))
