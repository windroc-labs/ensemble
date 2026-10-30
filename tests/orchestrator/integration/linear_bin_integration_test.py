"""bin/ensemble-orchestrator run by absolute path from a temp cwd, no PYTHONPATH, a minimal PATH."""

import json
import os
import re
import subprocess

from tests.orchestrator.case import REPO, Case
from tests.orchestrator.config_fixtures import CORE_LOCAL, PIPELINE
from tests.orchestrator.integration.fake_python import write_shim
from tests.orchestrator.unit.board_ids import MANAGER_LINE, TEAM
from tests.utils.bin_fixtures import MINIMAL, minimal, probe_copy, run
from tests.utils.case import local_section
from tests.utils.sandbox import scratch

BIN = os.path.join(REPO, "bin", "ensemble-orchestrator")
COMMANDS = [
    "router",
    "promote",
    "prune",
    "issue",
    "credentials",
    "status",
    "show",
    "hide",
    "close",
    "resume",
    "content",
    "writeback",
]


class Command(Case):
    def setUp(self):
        self.tmp = scratch(self)

    def run_bin(self, path, *argv, bin_path=BIN):
        return run(bin_path, path, *argv, cwd=self.tmp, home=self.home)

    def test_help_lists_the_subcommands(self):
        done = self.run_bin(minimal(self.tmp), "--help")
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertEqual(re.search(r"\{([a-z,-]+)\}", done.stdout)[1].split(","), COMMANDS)

    def test_router_help_exits_0(self):
        done = self.run_bin(minimal(self.tmp), "router", "--help")
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertIn("ensemble-orchestrator router", done.stdout)

    def test_it_sets_pythonpath_to_four_roots_and_puts_its_dir_first_on_path(self):
        copy = probe_copy(self.tmp, BIN)
        bin_dir = os.path.dirname(copy)
        done = self.run_bin(MINIMAL, "status", "TASK-1", bin_path=copy)
        self.assertEqual(done.returncode, 0, done.stderr)
        roots = os.path.join(self.tmp, "repo", "src")
        self.assertEqual(
            done.stdout.splitlines(),
            [f"{bin_dir}:{MINIMAL}", roots, "-P -m ensemble.orchestrator.cli status TASK-1"],
        )


class ConfigLayout(Command):
    """A temp HOME whose config.local.toml sets every required Linear setting under
    [orchestrator.linear]. Its python3, also every descendant's (ENSEMBLE_PATH), is fake_python.py:
    the Keychain is the fake's, holding no item, and a real `security` call is refused.
    """

    def setUp(self):
        super().setUp()
        self.fake = write_shim(os.path.join(self.tmp, "fake"))
        self.transport = os.path.join(self.tmp, "transport.json")
        with open(self.transport, "w", encoding="utf-8") as f:
            json.dump({"url": "http://127.0.0.1:9", "tokens": {}}, f)

    def write_local(self, orchestrator):
        with open(os.path.join(self.ensemble, "config.local.toml"), "w", encoding="utf-8") as f:
            f.write(CORE_LOCAL + "\n" + local_section(orchestrator, "orchestrator"))

    def run_fake(self, *argv):
        done = subprocess.run(
            (BIN, *argv),
            cwd=self.tmp,
            env={
                "HOME": self.home,
                "PATH": f"{self.fake}:{MINIMAL}",
                "ENSEMBLE_PATH": self.fake,
                "FAKE_LINEAR_TRANSPORT": self.transport,
            },
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertNotIn("refused a real Keychain call", done.stderr)
        return done

    def test_router_dry_run_and_issue_read_stop_at_their_keychain_items(self):
        self.write_local(MANAGER_LINE + PIPELINE)
        router = self.run_fake("router", "--dry-run")
        issue = self.run_fake("issue", "read", "TASK-1")
        for done in (router, issue):
            with self.subTest(argv=done.args[1:]):
                self.assertNotEqual(done.returncode, 0)
                self.assertNotIn("orchestrator/config.toml:", done.stdout + done.stderr)
                self.assertNotIn("config.local.toml:", done.stdout + done.stderr)
        # the fake's "no item": the harness app has no token, and a dry run requests none
        self.assertEqual(router.returncode, 2)
        self.assertIn("Linear harness app has no token and renewal failed", router.stderr)
        self.assertEqual((issue.returncode, issue.stdout), (2, ""))
        self.assertIn("Linear manager app has no token and renewal failed", issue.stderr)

    def test_a_linear_key_left_flat_is_refused(self):
        self.write_local(f'team = "{TEAM}"\n' + PIPELINE)
        for argv in (("router", "--dry-run"), ("issue", "read", "TASK-1")):
            with self.subTest(argv=argv):
                done = self.run_fake(*argv)
                self.assertNotEqual(done.returncode, 0)
                self.assertIn("unknown key 'team' in [orchestrator]", done.stderr)
