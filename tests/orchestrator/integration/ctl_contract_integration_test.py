"""The linear ctl contract on the real router and query entry, with private tmux and fake APIs."""

import os
import shlex
import subprocess
import sys
from pathlib import Path

from ensemble.utils import SID_ENV, Record
from tests.orchestrator.case import PY
from tests.orchestrator.integration.flow_fixtures import ENTRY, PROJECT, TmuxFlow
from tests.orchestrator.unit.board_ids import role_id
from tests.session import live_tmux
from tests.session.integration.clients.claude import fake_claude
from tests.utils.contracts.ctl import CtlContract


class LinearCtlContract(CtlContract, TmuxFlow):
    command = "ensemble-orchestrator"

    def setUp(self):
        super().setUp()
        self.started_records = {}
        self.scene([{"kind": "sleep", "seconds": 120}])
        probe = Path(self.root, "probe.json")
        probe.write_text('{"steps": []}', encoding="utf-8")
        Path(self.bin, "claude").write_text(
            "#!/bin/sh\n"
            "is_run=false\n"
            'for arg; do case "$arg" in --session-id|--resume) is_run=true;; esac; done\n'
            'if [ "$is_run" = false ]; then\n'
            f"  export FAKE_CLAUDE_SCENARIO={shlex.quote(str(probe))}\n"
            "fi\n"
            f'exec {shlex.quote(sys.executable)} -I {shlex.quote(fake_claude.__file__)} "$@"\n',
            encoding="utf-8",
        )

    def call(self, *argv, stdin="", sid=None):
        env = {**self.env(), **self.in_manager()}
        env.pop(SID_ENV, None)
        if sid is not None:
            env[SID_ENV] = sid
        return subprocess.run(
            (*PY, ENTRY, *argv),
            cwd=self.root,
            env=env,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def start(self, name, sid=None):
        issue = "TASK-" + name.removeprefix("w")
        self.fake.issue(
            "ENG: Contract",
            identifier=issue,
            delegate=role_id("engineer"),
            project=PROJECT,
            description="Exercise the ctl contract.",
        )
        done = self.call(
            "router",
            "--issue",
            issue,
            "--tui",
            "--events",
            os.path.join(self.root, "events"),
            sid=sid,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(len(done.stdout.splitlines()), 1, done.stdout)
        record = Record.parse(done.stdout)
        self.started_records[name] = record
        live_tmux.wait(
            lambda: any(
                path.stat().st_size
                for path in Path(self.root, "claude-config", "projects").glob(
                    f"*/{record.sid}.jsonl"
                )
            ),
            what="the contract's client transcript",
        )
        return record

    def inventory(self):
        return self.server.tmux("list-sessions", "-F", "#{session_name}").stdout

    def refuse(self):
        return self.call("router", "--issue", "TASK-9999")

    def end(self, name):
        for session in self.started_records[name].sessions:
            self.server.tmux("kill-session", "-t", f"={session}")
        live_tmux.wait(
            lambda: not any(self.live(session) for session in self.started_records[name].sessions),
            what="the contract's run to stop",
        )
