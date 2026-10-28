"""The act-as prompt command delivers the Composer's exact bytes for each role and destination,
without session, outcome or event files. Session options and stdout delivery are rejected. The
engineer prompt's team tool commands require step 4's prefix.
"""

import json
import os
import re
import shlex
import subprocess
import unittest

from ensemble.delegate.composer import Composer, RunParams
from ensemble.delegate.engine.types import Layout
from tests.delegate.case import REPO as CHECKOUT
from tests.delegate.case import Case
from tests.delegate.skills.act_as import argv, head, prefix
from tests.delegate.verifier import VERIFIER
from tests.session import harnesses
from tests.utils.case import local_section
from tests.utils.sandbox import scratch

SID = "0f8fad5b-d9cb-469f-a165-70867728950e"
ROLES = ("researcher", "pm", "engineer", "verifier", "dummy-tester")
INPUT = "Compare A and B."
ENSEMBLE = re.compile(r"python3 -P (?:-m ensemble\.[\w.]+|<team>/tools/[\w-]+\.py) \w+")
REPO = "python3 -P <team>/tools/repo.py"


class Instruction(Case):
    def setUp(self):
        self.proj = scratch(self)
        self.addCleanup(harnesses.end, self.home)
        self.adhoc = os.path.join(self.ensemble, "adhoc")
        self.local = os.path.join(self.ensemble, "config.local.toml")
        self.config()
        self.env = {
            "PATH": os.pathsep.join(
                (os.path.join(CHECKOUT, "bin"), os.environ.get("PATH", os.defpath))
            ),
            "HOME": self.home,
            "PYTHONUTF8": "1",
        }

    def work(self, role) -> str:
        return os.path.join(self.adhoc, f"2026-01-02-{role}-compare-a-b")

    def config(self, role=None, destination=None):
        text = f"trusted_dirs = [{json.dumps(self.proj)}]\n"
        if role is None:
            text += VERIFIER
        else:
            text += f"[roles.{role}.output]\n"
            text += "".join(f"{key} = {json.dumps(value)}\n" for key, value in destination.items())
        with open(self.local, "w", encoding="utf-8") as f:
            f.write(local_section(text, "delegate"))

    def cli(self, role, *extra, out=None) -> subprocess.CompletedProcess:
        work = self.work(role)
        args = ["prompt", "--role", role, "--workdir", work]
        args += ["--out", out or os.path.join(work, "prompt.md"), "--input", INPUT]
        return subprocess.run(
            [*argv(head()), *args, *extra],
            cwd=self.proj,
            env=self.env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            timeout=60,
        )

    def expected(self, role) -> bytes:
        work = self.work(role)
        composition = Composer()
        run = composition.resolve(role)
        params = RunParams(
            input=INPUT, out=os.path.join(work, "prompt.md"), workdir=Layout(work).agent
        )
        return composition.render(run, params).encode("utf-8")

    def delivered(self, role):
        out = os.path.join(self.work(role), "prompt.md")
        want = self.expected(role)
        res = self.cli(role)
        self.assertEqual((res.returncode, res.stdout), (0, ""), res.stderr)
        self.assertEqual(res.stderr, f"delegate: instruction delivered: {out}\n")
        with open(out, "rb") as f:
            self.assertEqual(f.read(), want)
        self.assertEqual(set(os.listdir(self.work(role))), {"prompt.md"})

    def test_each_role_receives_the_composers_prompt_with_its_destination(self):
        for role in ROLES:
            for destination in (
                None,
                {"type": "local"},
                {"type": "github", "repo": "o/docs", "branch": "main", "dir": "reports"},
                {"type": "pull-request"},
            ):
                with self.subTest(role=role, destination=destination):
                    self.config(None if destination is None else role, destination)
                    self.delivered(role)

    def test_the_engineer_prompts_tool_commands_run_behind_step_4s_prefix_only(self):
        self.delivered("engineer")
        with open(os.path.join(self.work("engineer"), "prompt.md"), encoding="utf-8") as f:
            commands = sorted(set(ENSEMBLE.findall(f.read())))
        self.assertEqual(commands, [f"{REPO} status", f"{REPO} worktree"])
        env = {"PATH": self.env["PATH"], "HOME": self.home}
        for command in commands:
            with self.subTest(command=command):
                command = command.replace("<team>", Composer().team.root)
                ok, bare = (
                    subprocess.run(
                        args,
                        cwd=self.proj,
                        env=env,
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        encoding="utf-8",
                        timeout=60,
                    )
                    for args in (
                        argv(f"{prefix()} {command} --help"),
                        shlex.split(f"{command} --help"),
                    )
                )
                self.assertEqual(ok.returncode, 0, ok.stderr)
                self.assertTrue(ok.stdout.startswith(f"usage: repo.py {command.split()[-1]} "))
                self.assertNotEqual(bare.returncode, 0)
                self.assertIn("No module named 'ensemble'", bare.stderr)

    def test_session_options_exit_2_without_writes(self):
        for extra in (
            ("--mode", "tui"),
            ("--client", "claude"),
            ("--sid", SID),
            ("--resume",),
            ("--detach",),
        ):
            with self.subTest(extra=extra):
                res = self.cli("researcher", *extra)
                self.assertEqual((res.returncode, res.stdout), (2, ""))
                self.assertIn(f"unrecognized arguments: {' '.join(extra)}", res.stderr)
                self.assertFalse(os.path.exists(self.adhoc))

    def test_stdout_delivery_exits_2_without_writes(self):
        res = self.cli("researcher", out="-")
        self.assertEqual((res.returncode, res.stdout), (2, ""))
        self.assertEqual(res.stderr, "delegate: --out takes a PATH, not -\n")
        self.assertFalse(os.path.exists(self.adhoc))


if __name__ == "__main__":
    unittest.main()
