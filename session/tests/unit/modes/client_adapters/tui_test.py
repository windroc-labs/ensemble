"""Tui contract tests."""

import inspect

from ensemble.session.engine.types import (
    Activity,
    Blocked,
    ClientBound,
    ClientContext,
    HookKind,
    Key,
    Paste,
    Permission,
    TurnEnd,
)
from ensemble.session.modes.client_adapters import tui
from ensemble.session.modes.client_adapters.tui import Tui

from session.tests.case import Case
from session.tests.client_adapters_support import DetailedTui, LabeledTui, StubTui


class NativeIdTui(LabeledTui):
    def session_id(self, payload):
        return payload.get("thread", "")


class TuiTest(Case):
    def setUp(self):
        self.run = DetailedTui()
        self.end = {"session_id": "n1", "last": "done", "tasks": [1, 2]}

    def test_defaults(self):
        run = StubTui()
        self.assertEqual(Tui.hooks, {})
        self.assertEqual(run._context, ClientContext("127.0.0.1", Permission.AUTO))
        self.assertEqual(
            (run.pending(self.end), run.activity({"doing": "x"}), run.blocked_reason({"why": "y"})),
            (0, "", ""),
        )
        for screen in ("", "limit reached\n> "):
            with self.subTest(screen=screen):
                self.assertIsNone(run.limit(screen))

    def test_submit_pastes_then_presses_enter(self):
        self.assertEqual(self.run.submit("x"), (Paste("x"), Key("Enter")))

    def test_session_id_is_the_payloads_session_id_when_a_str(self):
        for payload, native in (
            ({"session_id": "n1"}, "n1"),
            ({"session_id": ""}, ""),
            ({"session_id": 5}, ""),
            ({"session_id": None}, ""),
            ({}, ""),
        ):
            with self.subTest(payload=payload):
                self.assertEqual(self.run.session_id(payload), native)

    def test_a_new_session_id_binds_it_first_once_per_tui(self):
        self.assertEqual(
            self.run.hook_events("end", self.end), [ClientBound("n1"), TurnEnd("done", 2)]
        )
        self.assertEqual(self.run.hook_events("end", self.end), [TurnEnd("done", 2)])
        self.assertEqual(
            self.run.hook_events("end", {**self.end, "session_id": "n2"}),
            [ClientBound("n2"), TurnEnd("done", 2)],
        )
        self.assertEqual(
            DetailedTui().hook_events("end", self.end),
            [ClientBound("n1"), TurnEnd("done", 2)],
        )

    def test_no_session_id_binds_nothing(self):
        for session in ({}, {"session_id": ""}, {"session_id": 5}):
            with self.subTest(session=session):
                self.assertEqual(
                    self.run.hook_events("end", {"last": "r", **session}), [TurnEnd("r", 0)]
                )

    def test_each_label_gives_the_event_of_its_kind(self):
        payload = {"session_id": "n1", "last": "r", "tasks": [1], "doing": "Bash", "why": "ask"}
        self.run.hook_events("other", payload)
        for label, event in (
            ("end", TurnEnd("r", 1)),
            ("wait", Blocked("ask")),
            ("work", Activity("Bash")),
        ):
            with self.subTest(label=label):
                self.assertEqual(self.run.hook_events(label, payload), [event])

    def test_the_kinds_default_details(self):
        run = LabeledTui()
        payload = {"last": "r", "tasks": [1], "doing": "Bash", "why": "ask"}
        for label, event in (
            ("end", TurnEnd("r", 0)),
            ("wait", Blocked("")),
            ("work", Activity("")),
        ):
            with self.subTest(label=label):
                self.assertEqual(run.hook_events(label, payload), [event])

    def test_an_unknown_label_gives_nothing_more(self):
        self.assertEqual(self.run.hook_events("other", {"session_id": "n1"}), [ClientBound("n1")])
        for label in ("other", "", "End", HookKind.TURN_END, "Stop", "PostToolUse"):
            with self.subTest(label=label):
                self.assertEqual(self.run.hook_events(label, {"session_id": "n1", "last": "r"}), [])

    def test_without_hooks_no_label_has_a_kind(self):
        run = StubTui()
        payload = {"last": "r", "tool_name": "Bash"}
        for label in ("Stop", "PostToolUse", "Notification", "PermissionRequest", "end"):
            with self.subTest(label=label):
                self.assertEqual(run.hook_events(label, payload), [])

    def test_base_names_no_client_hook(self):
        source = inspect.getsource(tui)
        for name in (
            "Stop",
            "PreToolUse",
            "PostToolUse",
            "Notification",
            "PermissionRequest",
            "UserPromptSubmit",
        ):
            with self.subTest(name=name):
                self.assertNotRegex(source, rf"\b{name}\b")

    def test_hook_events_binds_the_id_session_id_reads(self):
        run = NativeIdTui()
        self.assertEqual(
            run.hook_events("end", {"thread": "t1", "session_id": "n1", "last": "r"}),
            [ClientBound("t1"), TurnEnd("r", 0)],
        )
        self.assertEqual(run.hook_events("end", {"thread": "t1", "last": "r"}), [TurnEnd("r", 0)])
        self.assertEqual(run.hook_events("work", {"session_id": "n1"}), [Activity("")])
