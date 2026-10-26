"""Crew close-all's session controls reconcile capacity while other sessions end."""

import os
from types import SimpleNamespace
from unittest import mock

from ensemble.manager.engine.crew import Crew
from ensemble.manager.engine.roster import Roster
from ensemble.manager.engine.types import Spec
from ensemble.utils import Record
from tests.session.hosted_support import HostedTuiBase


class CloseAllCapacityTest(HostedTuiBase):
    def test_close_all_succeeds_when_another_session_ends_during_inventory(self):
        self.configure(tui=True, max_runs=2)
        sessions = [self.start(mode="tui", name=name) for name in ("one", "two")]
        roster = Roster(os.path.join(self.ensemble, "managers", "manager"))
        for session in sessions:
            record = Record(
                1,
                "fake",
                session.name,
                (session.name,),
                session.sid,
                self.home,
                ("ensemble-session",),
                ("ensemble-session", "resume", session.name),
                None,
                None,
            )
            roster.begin(Spec(record.name, ("unused",)), sid=record.sid)
            roster.commit(record.name, record)
        ended = self.host.create("z-ending", metadata={"client": "fake", "sid": "old"})

        def ending(name):
            if name == "z-ending":
                ended.close()

        self.host.calls.attach.side_effect = ending
        closed = []

        def control(argv, **options):
            if argv[-1] == "--descendants":
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            self.assertEqual(argv[-1], "close")
            for line in options["input"].splitlines():
                record = Record.parse(line)
                self.session.attach(record.name, sid=record.sid).close()
                closed.append(record.name)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch("subprocess.run", side_effect=control):
            self.assertEqual(Crew(roster).close_all(), ["one", "two"])
        self.assertEqual(closed, ["one", "two"])
        self.assertEqual(roster.entries(), {})
        self.assertEqual(self.host.list(), [])
        ended.calls.close.assert_called_once_with()
        self.start(mode="tui", name="next").close()
