"""Crew CLI lease identity validation and holder command shape."""

import contextlib
import json
import os
import subprocess
from pathlib import Path
from unittest import mock

from ensemble.manager import Crew, Spec, State
from ensemble.manager.engine.crew import DESCENDANTS
from ensemble.manager.engine.roster import Roster

from manager.tests.cli_case import CliCase
from manager.tests.starters import answered
from utils.tests.fakes import ctl


class CliTest(CliCase):
    def test_force_forget_is_explicit_and_needs_no_controller(self):
        command = self.setup_ctl()
        Crew(self.roster).run(Spec("worker", (command, "start", "worker")))
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("missing ctl")):
            result, _, stderr = self.invoke("forget", "worker", "--manager", "mgr")
            self.assertEqual(result, 1, stderr)
            self.assertIn("worker", self.roster.entries())
            result, _, stderr = self.invoke("forget", "worker", "--manager", "mgr", "--force")
            self.assertEqual(result, 0, stderr)
        self.assertEqual(self.roster.entries(), {})

    def test_empty_run_note_and_note_command_store_null(self):
        command = self.setup_ctl()
        result, _, stderr = self.invoke(
            "run", "worker", "--manager", "mgr", "--note", "", "--", command, "start", "worker"
        )
        self.assertEqual(result, 0, stderr)
        self.assertIsNone(self.roster.entries()["worker"].note)
        result, _, stderr = self.invoke("note", "worker", "kept", "--manager", "mgr")
        self.assertEqual(result, 0, stderr)
        self.assertEqual(self.roster.entries()["worker"].note, "kept")
        result, _, stderr = self.invoke("note", "worker", "", "--manager", "mgr")
        self.assertEqual(result, 0, stderr)
        self.assertIsNone(self.roster.entries()["worker"].note)

    def test_run_reports_original_starter_failure_and_failed_lost_mark(self):
        command = self.setup_ctl()
        real_run = subprocess.run
        with self.roster.hold("old holder") as lease, contextlib.ExitStack() as takeover:

            def run(argv, **options):
                if tuple(argv) == (command, "start", "worker"):
                    takeover.enter_context(Roster(self.directory).hold("new holder", force=True))
                    raise FileNotFoundError("original starter failure")
                return real_run(argv, **options)

            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "ENSEMBLE_HOLDER_ID": lease.id,
                        "ENSEMBLE_HOLDER_GENERATION": str(lease.generation),
                    },
                ),
                mock.patch("subprocess.run", side_effect=run),
                mock.patch(
                    "subprocess.Popen", side_effect=answered(run, (command, "start", "worker"))
                ),
            ):
                result, stdout, stderr = self.invoke(
                    "run", "worker", "--manager", "mgr", "--", command, "start", "worker"
                )
            self.assertEqual(result, 1, stderr)
            self.assertEqual(stdout, "")
            self.assertIn("original starter failure", stderr)
            self.assertIn("stale holder generation", stderr)
            self.assertIs(self.roster.entries()["worker"].state, State.STARTING)

    def test_resume_reports_original_starter_failure_and_failed_gone_mark(self):
        command = self.setup_ctl()
        Crew(self.roster).run(Spec("worker", (command, "start", "worker")))
        ctl.set_session(self.ctl_dir, "worker", alive=False, state="gone")
        self.roster.mark("worker", State.GONE)
        real_run = subprocess.run
        with self.roster.hold("old holder") as lease, contextlib.ExitStack() as takeover:

            def run(argv, **options):
                if tuple(argv) == (command, "resume", "worker"):
                    takeover.enter_context(Roster(self.directory).hold("new holder", force=True))
                    raise FileNotFoundError("original starter failure")
                return real_run(argv, **options)

            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "ENSEMBLE_HOLDER_ID": lease.id,
                        "ENSEMBLE_HOLDER_GENERATION": str(lease.generation),
                    },
                ),
                mock.patch("subprocess.run", side_effect=run),
                mock.patch(
                    "subprocess.Popen", side_effect=answered(run, (command, "resume", "worker"))
                ),
            ):
                result, stdout, stderr = self.invoke("resume", "worker", "--manager", "mgr")
            self.assertEqual(result, 1, stderr)
            self.assertEqual(stdout, "")
            self.assertIn("original starter failure", stderr)
            self.assertIn("stale holder generation", stderr)
            self.assertIs(self.roster.entries()["worker"].state, State.RESUMING)

    def test_partial_close_all_exits_one_and_only_without_dry_run_closes_known_work(self):
        command = self.setup_ctl()
        Crew(self.roster).run(Spec("owned", (command, "start", "owned")))
        ctl.set_session(self.ctl_dir, "unknown", alive=True)
        before = Path(self.roster.path).read_bytes()
        native_run = subprocess.run

        def run(argv, **options):
            if tuple(argv) == DESCENDANTS:
                return subprocess.CompletedProcess(argv, 1, "", "listing unavailable")
            return native_run(argv, **options)

        # The dry run goes first: it leaves the roster and sessions as the other case starts them.
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                with mock.patch("subprocess.run", side_effect=run):
                    result, stdout, stderr = self.invoke(
                        "close-all", "--manager", "mgr", *(("--dry-run",) if dry_run else ())
                    )
                self.assertEqual(result, 1, stderr)
                self.assertEqual(stdout, "")
                self.assertIn("listing unavailable", stderr)
                self.assertIn("close-all partial", stderr)
                with Path(self.ctl_dir, ctl.SESSIONS).open(encoding="utf-8") as stream:
                    sessions = json.load(stream)
                self.assertTrue(sessions["unknown"]["alive"])
                closes = [call for call in ctl.calls(self.ctl_dir) if call["argv"] == ["close"]]
                if dry_run:
                    self.assertEqual(Path(self.roster.path).read_bytes(), before)
                    self.assertTrue(sessions["owned"]["alive"])
                    self.assertEqual(closes, [])
                else:
                    self.assertEqual(self.roster.entries(), {})
                    self.assertFalse(sessions["owned"]["alive"])
                    self.assertEqual(closes, [{"argv": ["close"], "names": ["owned"]}])
