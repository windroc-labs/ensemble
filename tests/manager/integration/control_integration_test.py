"""Crew control permissions and lock boundaries through executable fake commands."""

import fcntl
import os
import subprocess
from pathlib import Path
from unittest import mock

from ensemble.manager.engine.crew import Crew
from ensemble.manager.engine.roster import Roster
from ensemble.manager.engine.types import Held, Spec
from ensemble.utils import Status
from tests.manager.case import Case, child_env
from tests.utils.fakes import ctl


class Control(Case):
    def setUp(self):
        self.command = self.setup_ctl()
        self.directory = os.path.join(self.home, "roster")
        self.roster = Roster(self.directory)
        crew = Crew(self.roster)
        self.records = [
            crew.run(Spec(name, (self.command, "start", name))) for name in ("first", "second")
        ]

    def assert_unchanged_and_alive(self, entries, ledger):
        self.assertEqual(self.roster.entries(), entries)
        self.assertEqual(Path(self.roster.path).read_bytes(), ledger)
        result = subprocess.run(
            (self.command, "status"),
            input="".join(record.to_line() + "\n" for record in self.records),
            env=child_env(),
            capture_output=True,
            text=True,
            check=True,
        )
        statuses = [Status.parse(line) for line in result.stdout.splitlines()]
        self.assertEqual([status.name for status in statuses], ["first", "second"])
        self.assertTrue(all(status.alive for status in statuses))
        self.assertEqual(
            [call for call in ctl.calls(self.ctl_dir) if call["argv"][0] == "close"], []
        )

    def test_a_non_holders_close_and_close_all_close_nothing(self):
        with self.roster.hold("owner"):
            entries = self.roster.entries()
            ledger = Path(self.roster.path).read_bytes()
            other = Crew(Roster(self.directory))
            for method, args in (("close", ("first",)), ("close_all", ())):
                with self.subTest(method=method):
                    with self.assertRaises(Held):
                        getattr(other, method)(*args)
                    self.assert_unchanged_and_alive(entries, ledger)

    def test_status_and_show_execute_outside_roster_lock(self):
        for record in self.records:
            ctl.set_session(self.ctl_dir, record.name, showable=True)
        seen = []
        with self.listed():
            actual_run = subprocess.run

            def checked(argv, **options):
                if argv[0] == self.command and argv[1] in {"status", "show"}:
                    fd = os.open(Path(self.directory, "roster.lock"), os.O_RDWR)
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        seen.append(argv[1])
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    finally:
                        os.close(fd)
                return actual_run(argv, **options)

            with mock.patch.object(subprocess, "run", side_effect=checked):
                output = Crew(self.roster).attach()
        self.assertEqual(seen, ["status", "show"])
        calls = [call for call in ctl.calls(self.ctl_dir) if call["argv"][0] in {"status", "show"}]
        self.assertEqual([call["argv"][0] for call in calls], ["status", "show"])
        self.assertTrue(all(call["names"] == ["first", "second"] for call in calls))
        self.assertIn("first", output)
        self.assertIn("second", output)
