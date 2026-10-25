"""A killed Crew leaves a durable SID intent and a usable child stdout receipt."""

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ensemble.manager.engine.crew import RECOVERY_AGE, Crew
from ensemble.manager.engine.roster import Roster
from ensemble.manager.engine.types import State

from manager.tests.case import Case, child_env
from utils.tests.sandbox import reap

STARTER = """
import os, pathlib, subprocess, sys, time
record = subprocess.run(['ensemble-orchestrator', 'start', 'actual'], check=True, capture_output=True, text=True).stdout
sys.stdout.write(record)
sys.stdout.flush()
pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))
while not pathlib.Path(sys.argv[2]).exists(): time.sleep(.01)
sys.stdout.write(record)
sys.stdout.flush()
pathlib.Path(sys.argv[3]).write_text('finished')
"""
RUNNER = """
import sys
from ensemble.manager import Crew, Roster, Spec
Crew(Roster(sys.argv[1])).run(Spec('alias', tuple(sys.argv[2:]), 'keep'))
"""


class Receipt(Case):
    def wait_for(self, path: Path) -> None:
        deadline = time.monotonic() + 10
        while not path.exists():
            if time.monotonic() > deadline:
                self.fail(f"timed out waiting for {path}")
            time.sleep(0.01)

    def test_child_stdout_and_alias_survive_crew_death_before_commit(self):
        self.setup_ctl()
        directory = os.path.join(self.home, "roster")
        emitted, gate, finished = [
            Path(self.home, name) for name in ("emitted", "gate", "finished")
        ]
        argv = (sys.executable, "-P", "-S", "-c", STARTER, str(emitted), str(gate), str(finished))
        child = subprocess.Popen(
            (sys.executable, "-P", "-S", "-c", RUNNER, directory, *argv),
            env=child_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(reap, child, timeout=5)
        self.addCleanup(gate.touch)
        try:
            self.wait_for(emitted)
            roster = Roster(directory)
            entry = roster.entries()["alias"]
            record = roster.receipt_record("alias")
            self.assertEqual(
                (entry.state, entry.sid, record.sid), (State.STARTING, record.sid, record.sid)
            )
            self.assertEqual(record.name, "actual")
            child.kill()
            child.communicate(timeout=5)
            data = json.loads(Path(roster.path).read_text())
            data["entries"]["alias"]["started"] = (
                datetime.now(UTC) - timedelta(seconds=RECOVERY_AGE + 1)
            ).isoformat()
            Path(roster.path).write_text(json.dumps(data))
            with self.listed([record]):
                Crew(roster).attach()
            adopted = roster.entries()["alias"]
            self.assertEqual(
                (adopted.state, adopted.record, adopted.note), (State.LIVE, record, "keep")
            )
            self.assertEqual(list(roster.entries()), ["alias"])
            self.assertTrue(Path(directory, entry.receipt).exists())
            gate.touch()
            self.wait_for(finished)
            deadline = time.monotonic() + 5
            while roster.entries()["alias"].receipt is not None:
                if time.monotonic() > deadline:
                    self.fail("completed receipt was not cleaned")
                time.sleep(0.01)
            self.assertFalse(Path(directory, entry.receipt).exists())
        finally:
            gate.touch()
            reap(child, timeout=5)
            child.communicate(timeout=5)
