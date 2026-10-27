"""A Claude tui session's mid-turn answer: a `reply <id>` events line, before the turn's `done`,
whose stored text is the answer, on the tmux Host, with fake Claude and a live hook.
"""

import glob
import json
import os
import tempfile
import time
from pathlib import Path
from unittest import mock

from ensemble.session.engine.hosted_session import HostedSession
from ensemble.session.engine.types import Access, Mode, Spec
from ensemble.session.hosts.base import Host
from ensemble.utils import file_lock, read_reply
from tests.session import live_tmux
from tests.session.case import HOSTS, REPO, Case
from tests.session.integration.clients.claude import fake_claude

WAIT = 15
ANSWER = "half done\nnext: tests"


class RepliesTest(Case):
    host = "tmux"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("ENSEMBLE_TEST_TMP", REPO))
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home, self.work = self.root / "home", self.root / "work"
        self.config = self.home / ".claude"
        for path in (self.home, self.work, self.config):
            path.mkdir(parents=True, exist_ok=True)
        self.server = live_tmux.Server(self, str(self.home), bin_dir=str(self.root / "bin"))
        self.server.start("mgr")
        fake_claude.install(str(self.server.bin))
        self.commands, self.scenario = self.root / "commands.jsonl", self.root / "scenario.json"
        self.events = self.root / "events"
        env = self.server.env(CLAUDE_CONFIG_DIR=str(self.config), TZ="UTC")
        self.enterContext(mock.patch.dict(os.environ, {**env, fake_claude.ENV: str(self.scenario)}))
        clients = {"claude": {"implementation": "ensemble.session.clients.claude:ClaudeClient"}}
        hosts = Host.parse_hosts(
            {
                **HOSTS,
                "default": "tmux",
            }
        )
        self.session_cls = HostedSession.configured(clients=clients, hosts=hosts)
        self.release = self.root / "release"
        steps = [{"kind": "queued", "answer": ANSWER}, {"kind": "pause", "file": str(self.release)}]
        scenario = {"steps": [*steps, "final"], "commands": str(self.commands)}
        self.scenario.write_text(json.dumps(scenario))

    def start(self):
        spec = Spec(
            workdir=str(self.work),
            access=Access(cwd=str(self.work)),
            name="w1",
            client="claude",
            mode=Mode.TUI,
            prompt="hello",
            events=str(self.events),
        )
        handle = self.session_cls.start(spec, host=self.host)
        self.addCleanup(handle.stop)
        return handle

    def words(self):
        if not self.events.exists():
            return []
        return [line.split()[1:] for line in self.events.read_text().splitlines() if line.strip()]

    def until(self, word):
        return live_tmux.wait(
            lambda: next((w for w in self.words() if w[1:2] == [word]), None),
            WAIT,
            what=f"a {word} line",
        )

    def transcript(self) -> str:
        found = glob.glob(str(self.config / "projects" / "*" / "*.jsonl"))
        return Path(found[0]).read_text() if found else ""

    def interjected(self):
        """Sends a message while the turn's tool runs."""
        handle = self.start()
        live_tmux.wait(lambda: '"tool_use"' in self.transcript(), WAIT, what="the turn's tool")
        handle.send("status?")
        self.addCleanup(self.watcher_gone)
        return time.monotonic()

    def watcher_gone(self):
        """Waits out the watcher, which ends IDLE_EXIT s after its last answer."""

        def free():
            states = str(self.home / "**" / "replies" / "*.json")
            for path in glob.glob(states, recursive=True, include_hidden=True):
                try:
                    with file_lock(path + ".watch", wait=False):
                        pass
                except BlockingIOError:
                    return False
            return True

        live_tmux.wait(free, WAIT, what="the watcher's exit")

    def test_interjection_writes_reply_before_done(self):
        self.interjected()
        name, _, reply_id = self.until("reply")
        self.assertEqual(name, "w1")
        self.assertEqual(read_reply(str(self.events), reply_id), ANSWER)
        self.release.touch()
        self.until("done")
        words = [w[1] for w in self.words()]
        self.assertEqual(words.count("reply"), 1)
        self.assertLess(words.index("reply"), words.index("done"))

    def test_reply_without_tool_call_within_10s(self):
        sent = self.interjected()
        self.until("reply")
        self.assertLessEqual(time.monotonic() - sent, 10)
        self.assertNotIn("done", [w[1] for w in self.words()])
        self.release.touch()
