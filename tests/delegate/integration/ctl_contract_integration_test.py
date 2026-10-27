"""The delegate ctl contract through public commands and a gated fake client, each detached run's
drive a real local process.
"""

import contextlib
import json
import os
import sys
from pathlib import Path

from ensemble.utils import ProcessControl, Record
from tests.delegate.integration.cli import ASKING, Integration
from tests.delegate.replies import block, mark
from tests.session import live_tmux
from tests.utils.contracts.ctl import AnswerCtlContract

WAIT_FOR_PATH = """import sys, time
from pathlib import Path
deadline = time.monotonic() + 60
while not Path(sys.argv[1]).exists():
    if time.monotonic() >= deadline:
        sys.exit(1)
    time.sleep(0.02)
"""


class DelegateCtlContract(AnswerCtlContract, Integration):
    listed = False
    command = "ensemble-delegate"

    def setUp(self):
        super().setUp()
        self.started_records = {}

    def call(self, *argv, stdin="", sid=None):
        return self.control(*argv, stdin=stdin, sid=sid)

    def start(self, name, sid=None):
        self.work = os.path.join(self.root, name)
        gate = os.path.join(self.work, "release")
        self.scene(
            [
                mark("start", "waiting"),
                {
                    "kind": "run",
                    "argv": [sys.executable, "-c", WAIT_FOR_PATH, gate],
                    "path": os.path.join(self.work, "gate-result.json"),
                },
                block(ASKING),
            ]
        )
        done = self.call(
            "run",
            "--role",
            self.ROLE,
            "--input",
            "initial",
            "--workdir",
            self.work,
            "--detach",
            "--mode",
            "headless",
            "--events",
            os.path.join(self.root, "events"),
            sid=sid,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        record = Record.parse(done.stdout)
        self.started_records[name] = record
        self.addCleanup(self.stop_drive, record.workdir)
        live_tmux.wait(
            lambda: any(
                path.stat().st_size
                for path in Path(self.config, "projects").glob(f"*/{record.sid}.jsonl")
            ),
            what="the contract client transcript",
        )
        return record

    def inventory(self):
        """The tmux sessions and each run's drive."""
        self.control("status", self.work)
        sessions = self.resume_server.tmux("list-sessions", "-F", "#{session_name}").stdout
        runs = Path(self.root).glob("*/run/delegate.json")
        return sessions, {str(record): self.drive_of(record) for record in runs}

    def drive_of(self, record):
        """The drive control path delegate.json `record` names."""
        with open(record, encoding="utf-8") as file:
            return json.load(file).get("drive")

    def stop_drive(self, workdir):
        """A drive the test left running, killed before the temp dirs go."""
        with contextlib.suppress(OSError):
            path = self.drive_of(os.path.join(workdir, "run", "delegate.json"))
            control = None if path is None else ProcessControl.attach(path)
            if control is not None and control.running():
                control.kill()
                live_tmux.wait(lambda: not control.live(), what="the left drive gone")

    def refuse(self):
        return self.call(
            "run",
            "--role",
            "no-such-role",
            "--input",
            "initial",
            "--detach",
            "--mode",
            "headless",
            "--events",
            os.path.join(self.root, "events"),
        )

    def end(self, name):
        result = self.call("close", stdin=self.started_records[name].to_line())
        self.assertEqual(result.returncode, 0, result.stderr)

    def waiting(self, record):
        Path(record.workdir, "release").touch()
        self.completed()
        result = self.call("outcome", record.workdir)
        self.assertEqual(json.loads(result.stdout)["status"], "needs_input")
        self.scene([], hang=True)

    def input_of(self, record):
        def inputs():
            with open(os.path.join(record.workdir, "run", "run.jsonl"), encoding="utf-8") as stream:
                records = [json.loads(line) for line in stream]
            return [entry["text"] for entry in records if entry["kind"] == "input"]

        live_tmux.wait(lambda: len(inputs()) == 2, what="the answer input record")
        return inputs()[-1]
