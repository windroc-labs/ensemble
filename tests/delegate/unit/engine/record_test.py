"""RunRecord reads and writes delegate.json without the Session or ensemble config."""

import json
from dataclasses import replace

from ensemble.delegate import ConfigError, Outcome
from ensemble.delegate.engine.harness_delegate import HarnessDelegate
from ensemble.delegate.engine.record import RunRecord
from ensemble.delegate.engine.types import Layout, Limit, Result
from ensemble.session import HookCallback
from ensemble.utils import OutcomeStatus
from tests.delegate.harness import HarnessCase


class RunRecordTest(HarnessCase):
    def record(self) -> RunRecord:
        return RunRecord(Layout(self.work))

    def test_a_full_state_round_trips(self):
        self.make()
        record = self.record()
        state = record.read()
        session = replace(
            state.plan.session, handlers=(HookCallback("pkg.handler", {"worker": 1}, ("/r",)),)
        )
        outcome = Outcome(
            OutcomeStatus.DONE, "title", "summary", url="url", deliverable="deliverable"
        )
        full = replace(
            state,
            plan=replace(state.plan, session=session),
            completed=True,
            result=Result(2, outcome, "the client exited 2", Limit(12.5)),
        )
        record.save(full)
        self.assertEqual(record.read(), full)

    def test_a_bad_record_is_a_config_error(self):
        self.make()
        data = self.metadata()
        for key, value in (("version", 2), ("spec", {**data["spec"], "workdir": "/elsewhere"})):
            with self.subTest(key=key):
                with open(self.state_record(), "w", encoding="utf-8") as file:
                    json.dump({**data, key: value}, file)
                with self.assertRaisesRegex(ConfigError, "^invalid .*delegate.json: "):
                    self.record().read()
        with self.assertRaisesRegex(ConfigError, "missing metadata"):
            self.record().parse(None)

    def test_it_reads_and_writes_while_the_config_is_unreadable(self):
        self.make()
        with open(self.local, "w", encoding="utf-8") as file:
            file.write("not toml [")
        with self.assertRaises(ConfigError):
            HarnessDelegate.attach(self.work)
        record = self.record()
        record.save(record.read())
