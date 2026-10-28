"""Real CLI and child process transport, with no Docker daemon, client or credentials."""

import json
import shutil
import subprocess
from pathlib import Path

from ensemble.utils import Record
from tests.delegate.case import PY, REPO, Case, child_env
from tests.session.case import SID


class Open(Case):
    def setUp(self):
        self.cwd = Path(self.home, "work ; literal")
        self.cwd.mkdir()
        self.fake = Path(self.home, "session-python")
        shutil.copyfile(Path(REPO, "tests/delegate/fakes/direct_session.py"), self.fake)
        self.fake.chmod(0o755)
        self.log = Path(self.home, "session.json")
        self.config = Path(self.ensemble, "config.local.toml")
        self.config.write_text(
            '[session.hosts]\ndefault = "docker"\n'
            '[delegate.environments.dummy]\nimage = "dummy-env:test"\nlock = "dummy-lock"\n'
            '[[delegate.environments.dummy.credentials.env]]\nitem = "ensemble-broker-env/dummy"\nfile = "dummy.txt"\n'
        )

    def open(self, *extra, code="0", name="dummy-1"):
        return subprocess.run(
            (
                *PY,
                str(Path(REPO, "tests/delegate/fakes/open_launcher.py")),
                "open",
                "--environment",
                "dummy",
                *extra,
                "--",
                name,
            ),
            cwd=self.cwd,
            env={
                **child_env(),
                "HOME": self.home,
                "ENSEMBLE_SID": SID,
                "ENSEMBLE_EVENTS": str(Path(self.home, "events")),
                "FAKE_SESSION_PYTHON": str(self.fake),
                "FAKE_SESSION_LOG": str(self.log),
                "FAKE_SESSION_EXIT": code,
            },
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def test_open_prints_only_the_workers_record_and_preserves_identity(self):
        done = self.open("--account", "work", "--display", "none")
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        record = Record.parse(done.stdout)
        self.assertEqual(
            (record.kind, record.name, record.sid, record.sessions),
            ("worker", "dummy-1", SID, ("dummy-1",)),
        )
        self.assertEqual(record.resume, ("ensemble-session", "resume", "dummy-1"))
        launch = json.loads(self.log.read_text())
        self.assertEqual((launch["sid"], launch["events"]), (SID, str(Path(self.home, "events"))))
        argv = launch["argv"]
        self.assertEqual(argv[argv.index("--cwd") + 1], str(self.cwd))
        self.assertIn("--account=work", argv)
        spec = json.loads(argv[argv.index("--environment-spec") + 1])
        self.assertEqual(
            json.loads(argv[argv.index("--environment-credentials") + 1]),
            {"env": [{"item": "ensemble-broker-env/dummy", "file": "dummy.txt"}]},
        )
        self.assertIsNone(spec["prefetch"])
        self.assertFalse(list(Path(self.home).rglob("delegate.json")))

    def test_leading_dash_display_survives_both_cli_parsers(self):
        done = self.open("--display=-pane")
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertEqual(Record.parse(done.stdout).display, "-pane")

    def test_busy_has_no_record_and_preserves_stderr_and_exit_75(self):
        done = self.open(code="75")
        self.assertEqual((done.returncode, done.stdout), (75, ""))
        self.assertIn("dummy-lock is held by dummy-1 (owner dummy-1)", done.stderr)

    def test_prefetch_is_rejected_before_the_child_process(self):
        self.config.write_text(
            self.config.read_text().replace(
                'lock = "dummy-lock"',
                'lock = "dummy-lock"\nprefetch = true',
            )
        )
        done = self.open()
        self.assertEqual((done.returncode, done.stdout), (2, ""))
        self.assertIn("prefetch", done.stderr)
        self.assertFalse(self.log.exists())
