"""A Codex tui session's mid-turn answer: a `reply <id>` events line, before the turn's `done`,
whose stored text is the answer, on the tmux Host, with fake Codex and a live hook.
"""

import dataclasses
import json
import os
import tempfile
import time
from pathlib import Path
from unittest import mock

from ensemble.session.clients.codex.common import SessionStore
from ensemble.session.clients.codex.tui import replies
from ensemble.session.engine.hosted_session import HostedSession
from ensemble.session.engine.types import Access, Mode, Spec
from ensemble.session.hosts.base import Host
from ensemble.utils import file_lock, read_reply

from session.tests import live_tmux
from session.tests.case import HOSTS, REPO, Case
from session.tests.integration.clients.codex import fake_tui

WAIT = 15
ANSWER = "half done\nnext: tests"


class RepliesTest(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("ENSEMBLE_TEST_TMP", REPO))
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home, self.work = self.root / "home", self.root / "work"
        self.cwd, self.codex_home = self.work / "project", self.home / ".codex"
        for path in (self.home, self.cwd, self.codex_home):
            path.mkdir(parents=True, exist_ok=True)
        self.server = live_tmux.Server(self, str(self.home), bin_dir=str(self.root / "bin"))
        self.server.start("mgr")
        fake_tui.install(self.server.bin)
        self.log, self.scenario = self.root / "client.jsonl", self.root / "scenario.json"
        self.events = self.root / "events"
        env = self.server.env(CODEX_HOME=str(self.codex_home), TZ="UTC")
        self.enterContext(mock.patch.dict(os.environ, env, clear=True))
        clients = {
            "codex": {
                "implementation": "ensemble.session.clients.codex:CodexClient",
                "tiers": {"1": "gpt-test"},
                "efforts": {"low": "low"},
                "env": {"FAKE_LOG": str(self.log), "FAKE_SCENARIO": str(self.scenario)},
            }
        }
        hosts = Host.parse_hosts({**HOSTS, "default": "tmux"})
        self.session_cls = HostedSession.configured(clients=clients, hosts=hosts)
        self.release = self.root / "release"
        steps = [{"kind": "steer", "answer": ANSWER}, {"kind": "pause", "file": str(self.release)}]
        self.scenario.write_text(json.dumps({"tui_turns": [[*steps, "final"]]}))

    def start(self):
        spec = Spec(
            workdir=str(self.work),
            tier=1,
            effort="low",
            access=Access(cwd=str(self.cwd), project=True),
            name="w1",
            sid="caller-id",
            client="codex",
            mode=Mode.TUI,
            prompt="hello",
            events=str(self.events),
        )
        handle = self.session_cls.start(dataclasses.replace(spec))
        self.addCleanup(handle.stop)
        return handle

    def words(self):
        if not self.events.exists():
            return []
        return [line.split()[1:] for line in self.events.read_text().splitlines() if line.strip()]

    def until(self, word):
        found = live_tmux.wait(
            lambda: next((w for w in self.words() if w[1:2] == [word]), None),
            WAIT,
            what=f"a {word} line",
        )
        return found

    def steered(self):
        handle = self.start()
        live_tmux.wait(
            lambda: any(
                json.loads(line).get("hook") == "UserPromptSubmit"
                for line in (self.log.read_text().splitlines() if self.log.exists() else ())
            ),
            WAIT,
            what="the turn's first prompt hook",
        )
        handle.send("status?")
        self.addCleanup(self.watcher_gone)
        return time.monotonic()

    def watcher_gone(self):
        """Waits out the steer's watcher, which ends IDLE_EXIT s after its last answer."""
        path = replies.state_path(SessionStore().root, str(self.work), "caller-id")

        def free():
            try:
                with file_lock(path + ".watch", wait=False):
                    return True
            except BlockingIOError:
                return False

        live_tmux.wait(free, WAIT, what="the watcher's exit")

    def test_steer_writes_reply_before_done(self):
        self.steered()
        name, _, reply_id = self.until("reply")
        self.assertEqual(name, "w1")
        self.assertEqual(read_reply(str(self.events), reply_id), ANSWER)
        self.release.touch()
        self.until("done")
        words = [w[1] for w in self.words()]
        self.assertEqual(words.count("reply"), 1)
        self.assertLess(words.index("reply"), words.index("done"))

    def test_reply_without_tool_call_within_10s(self):
        sent = time.monotonic()
        self.steered()
        self.until("reply")
        self.assertLessEqual(time.monotonic() - sent, 10)
        self.assertNotIn("done", [w[1] for w in self.words()])
        self.release.touch()
