"""Private roster fixtures shared by logic and native-process tests."""

import json
import os
from pathlib import Path
from unittest import mock
from uuid import uuid4

from ensemble.manager.engine.roster import Roster
from ensemble.manager.engine.types import Spec, State
from ensemble.utils import Record
from tests.manager.case import Case


def mark(roster: Roster, name: str, state: State) -> None:
    """Roster entry `name` moved to `state` by the roster's own transition rule (a test's setup)."""
    with roster._locked() as data:
        roster._lease(data)
        roster._mark(data, name, State(state))


class RosterCase(Case):
    def setUp(self):
        self.enterContext(mock.patch("ensemble.manager.engine.events.SETTLE", 0))
        self.directory = os.path.join(self.ensemble, "managers", "manager")
        self.roster = Roster(self.directory)
        self.record = Record(
            1,
            "custom",
            "x",
            ("s1", "s2"),
            str(uuid4()),
            self.home,
            ("ensemble-session",),
            ("ensemble-session", "resume", "x"),
            "opaque",
            "detail",
        )

    def begin(self, name="x", note=None, sid=None):
        self.roster.begin(Spec(name, ("fake-ctl", "start", name), note), sid=sid or self.record.sid)

    def live(self, name="x", note=None, record=None):
        record = record or self.record
        self.begin(name, note, record.sid)
        self.roster.commit(name, record)

    def content(self):
        return json.loads(Path(self.roster.path).read_text())

    def _prepare_receipt(self, state):
        self.begin(note="keep")
        if state == State.RESUMING:
            self.roster.commit("x", self.record)
            mark(self.roster, "x", State.GONE)
            self.roster.claim_resume("x")
        self.assertIs(self.roster.entries()["x"].state, state)
