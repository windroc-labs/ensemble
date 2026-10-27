"""The session harness on a private tmux server with the fake claude: it owns each Claude tui
worker started on tmux, keeps supervising it after the starting process exits, hands its events to
registered handlers in order, and ends with a confirmed close.
"""

import json
import os
from unittest import mock

from ensemble.session.engine.hosted_session import HostedSession
from ensemble.session.engine.types import Access, HookCallback, Spec
from ensemble.session.harness.channel import Channel
from ensemble.session.harness.view import OwnedSession
from ensemble.session.hosts.base import Host
from tests.session import live_tmux
from tests.session.case import HOSTS, REPO
from tests.session.cli_support import WorkerCase, logged

PERMISSION = {"kind": "hook", "event": "PermissionRequest", "tool_name": "Edit"}
HANDLER = "tests.session.integration.harness.recording_handler"
DISPATCHED = ("TurnStarted", "Blocked", "TurnEnd", "Exited")


class HarnessCase(WorkerCase):
    def owner(self, name: str) -> Channel:
        path = os.path.join(self.ensemble, "tui", name, "session.json")
        with open(path, encoding="utf-8") as f:
            return Channel(json.load(f)["owner"])

    def logged_events(self, channel: Channel) -> list[dict]:
        return logged(channel.events)

    def wait_logged(self, channel: Channel, kind: str, count: int = 1) -> list[dict]:
        def found():
            records = self.logged_events(channel)
            return records if sum(r["type"] == kind for r in records) >= count else None

        return live_tmux.wait(found, timeout=30, what=f"{count} x {kind} in {channel.events}")


class OwnerTest(HarnessCase):
    def test_start_returns_once_the_harness_runs_it(self):
        record = self.record(self.start("w1"))
        channel = self.owner("w1")
        self.assertTrue(channel.alive())
        status = channel.status()
        self.assertEqual(
            (status["state"], status["name"], status["sid"]), ("running", "w1", record.sid)
        )

    def test_the_worker_stays_supervised_after_its_entry_exits_and_its_display_hides(self):
        self.record(self.start("w1", turns=[["Second."]]))
        channel = self.owner("w1")
        self.wait_logged(channel, "TurnEnd")
        self.ok(self.session("hide", "w1"))
        self.ok(self.session("send", "w1", "next"))
        records = self.wait_logged(channel, "TurnEnd", 2)
        ends = [r for r in records if r["type"] == "TurnEnd"]
        self.assertEqual([r["fields"]["reply"] for r in ends], ["Hi from w1.", "Second."])
        self.assertEqual([r["turn"] for r in ends], [ends[0]["turn"], ends[1]["turn"]])
        self.assertNotEqual(ends[0]["turn"], ends[1]["turn"])
        self.assertTrue(channel.alive())

    def test_close_ends_worker_and_harness(self):
        self.record(self.start("w1"))
        channel = self.owner("w1")
        self.ok(self.session("close", "w1"))
        self.assertFalse(self.live("w1"))
        self.assertEqual(self.logged_events(channel)[-1]["type"], "Exited")
        live_tmux.wait(lambda: not channel.alive(), timeout=40, what="the harness to exit")


class HandlerTest(HarnessCase):
    def test_a_handler_gets_turn_started_turn_end_blocked_and_exited_in_order(self):
        out = os.path.join(self.root, "handled.jsonl")
        steps = [PERMISSION, "Done."]
        env = {**self.env(), **self.scene("w1", steps), "ENSEMBLE_EVENTS": self.events}
        self.enterContext(mock.patch.dict(os.environ, env, clear=True))
        cls = HostedSession.configured(
            clients={"claude": {"implementation": "ensemble.session.clients.claude:ClaudeClient"}},
            hosts=Host.parse_hosts({"tmux": HOSTS["tmux"], "default": "tmux"}),
        )
        spec = Spec(
            workdir=self.cwd,
            access=Access(cwd=self.cwd),
            client="claude",
            mode="tui",
            name="w1",
            prompt="hi",
            handlers=(HookCallback(HANDLER, {"path": out}, (REPO,)),),
        )
        session = cls.start(spec)
        self.assertIsInstance(session, OwnedSession)
        assert isinstance(session, OwnedSession)
        live_tmux.wait(
            lambda: any(r["type"] == "TurnEnd" for r in logged(out)),
            timeout=30,
            what="the handler's TurnEnd",
        )
        channel = self.owner("w1")
        session.close()
        handled = live_tmux.wait(
            lambda: (found := logged(out)) and found[-1]["type"] == "Exited" and found,
            timeout=30,
            what="the handler's Exited",
        )
        kinds = [r["type"] for r in handled if r["type"] in DISPATCHED]
        self.assertEqual(kinds, ["TurnStarted", "Blocked", "TurnEnd", "Exited"])
        self.assertEqual([r["reply"] for r in handled if r["type"] == "TurnEnd"], ["Done."])
        ids = [r["event_id"] for r in handled]
        self.assertEqual(len(set(ids)), len(ids))
        self.assertTrue(all(i.startswith(session.generation + ":") for i in ids))
        self.assertEqual(
            [r["id"] for r in logged(channel.events) if r["type"] not in ("ClientBound", "Stderr")],
            ids,
        )
