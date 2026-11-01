"""OpenCode TUI sessions on a private tmux server: fake_tui.mjs (on PATH as `opencode`, under
Node) loads the real plugin.js and plays OpenCode's bus events through it to the hook command.
"""

import dataclasses
import json
import os
import shlex
import shutil
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from ensemble.session.engine.records import RunLog
from ensemble.session.engine.types import (
    Access,
    Activity,
    Blocked,
    ConfigError,
    Mode,
    Reply,
    Spec,
    StatusState,
    TurnEnd,
    TurnFailed,
    TurnStarted,
)
from tests.session import live_tmux
from tests.session.case import REPO, Case, configured
from tests.session.integration.clients.opencode import fake_opencode
from tests.session.tui_support import Stream
from tests.session.unit.clients.opencode.support import make_db
from tests.utils.jsonl import read_jsonl
from tests.utils.sandbox import scratch

WAIT = 20
NODE = shutil.which("node")
FAKE_TUI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_tui.mjs")
NATIVE = "ses_fakeTuiRootSession0001"


@unittest.skipIf(NODE is None, "node is not on PATH: the OpenCode TUI tests run plugin.js under it")
class TuiSessions(Case):
    def setUp(self):
        self.root = Path(scratch(self, os.environ.get("ENSEMBLE_TEST_TMP", REPO)))
        self.home, self.work = self.root / "home", self.root / "work"
        self.cwd = self.work / "project"
        for path in (self.home, self.cwd):
            path.mkdir(parents=True)
        self.server = live_tmux.Server(self, str(self.home), bin_dir=str(self.root / "bin"))
        self.server.start("mgr")
        shim = Path(self.server.bin) / "opencode"
        shim.write_text(
            "#!/bin/sh\n"
            f'case "$1" in run|--version) exec {shlex.quote(sys.executable)} '
            f'{shlex.quote(fake_opencode.__file__)} "$@";; esac\n'
            f'exec {shlex.quote(str(NODE))} {shlex.quote(FAKE_TUI)} "$@"\n'
        )
        shim.chmod(0o755)
        self.log, self.scenario, self.events = (
            self.root / n for n in ("client.jsonl", "scenario.json", "events")
        )
        self.scene()
        git = {
            k: os.environ[k]
            for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM")
            if k in os.environ
        }
        env = self.server.env(XDG_DATA_HOME=str(self.root / "data"), **git)
        self.enterContext(mock.patch.dict(os.environ, env, clear=True))
        self.clients = {
            "opencode": {
                "implementation": "ensemble.session.clients.opencode:OpenCodeClient",
                "tiers": {"1": "ollama/qwen3.6:35b-mlx"},
                "env": {"FAKE_LOG": str(self.log), "FAKE_SCENARIO": str(self.scenario)},
            }
        }
        self.session_cls = configured(self.clients)
        self.common = self.session_cls._config().registry.client("opencode").common
        self.spec = Spec(
            workdir=str(self.work),
            access=Access(cwd=str(self.cwd), project=True),
            sid="run-1",
            tier=1,
            client="opencode",
            mode=Mode.TUI,
            name="w1",
            events=str(self.events),
        )

    def scene(self, turns=None, **fields):
        self.scenario.write_text(json.dumps({"turns": turns or [[{"text": "first"}]], **fields}))

    def start(self, prompt="Hello", **fields):
        handle = self.session_cls.start(dataclasses.replace(self.spec, prompt=prompt, **fields))
        self.addCleanup(handle.stop)
        return handle, Stream(
            self, handle, poke=lambda: self.server.tmux("run-shell", "-b", "true")
        )

    def logged(self) -> list[dict]:
        return read_jsonl(self.log, missing_ok=True)

    def calls(self) -> list[dict]:
        return [row for row in self.logged() if row.get("mode") == "tui"]

    def words(self, count: int) -> list[str]:
        def check():
            self.server.tmux("run-shell", "-b", "true")
            rows = live_tmux.events(str(self.events))
            return rows if len(rows) >= count else None

        return [row.split(" ", 1)[1] for row in live_tmux.wait(check, WAIT, what="event lines")]

    def test_a_prompt_turn_ends_done_with_its_reply(self):
        self.scene([[{"text": "Hello back."}]])
        handle, stream = self.start()
        self.assertEqual(
            stream.until(TurnEnd),
            [TurnStarted(), Activity(""), Reply("Hello back."), TurnEnd("Hello back.")],
        )
        self.assertEqual(handle.reply, "Hello back.")
        self.assertEqual(self.words(1)[-1], "w1 done")
        (call,) = self.calls()
        self.assertEqual(call["argv"], ["--model", "ollama/qwen3.6:35b-mlx", "--prompt=Hello"])
        self.assertEqual(call["cwd"], str(self.cwd))
        self.assertEqual(call["config"]["permission"]["bash"], "ask")

    def test_a_send_works_then_ends_with_the_new_reply(self):
        self.scene([[{"text": "one"}], [{"tool": "read"}, {"text": "two"}]])
        handle, stream = self.start()
        stream.until(TurnEnd)
        handle.send("next")
        self.assertEqual(
            stream.until(TurnEnd),
            [
                TurnStarted(),
                Activity(""),
                Activity("read"),
                Activity("read"),
                Reply("two"),
                TurnEnd("two"),
            ],
        )
        self.assertEqual(handle.reply, "two")

    def test_sends_mid_turn_join_the_running_turn(self):
        """OpenCode's running loop takes a prompt saved while its turn runs (each once
        failed the turn as superseded); a send after the turn's idle starts the next one.
        """
        self.scene([[{"text": "one"}, {"join": 2}, {"text": "two"}], [{"text": "three"}]])
        handle, stream = self.start()
        self.assertEqual(stream.until(Reply), [TurnStarted(), Activity(""), Reply("one")])
        handle.send("also this")
        handle.send("and this")
        sent = time.monotonic()
        self.assertEqual(stream.until(TurnEnd), [Reply("two"), TurnEnd("two")])
        self.assertLess(time.monotonic() - sent, 4)  # no REPLY_WAIT for a reply after a join
        self.assertEqual(self.words(1), ["w1 done"])
        handle.send("next")
        self.assertEqual(
            stream.until(TurnEnd), [TurnStarted(), Activity(""), Reply("three"), TurnEnd("three")]
        )

    def test_a_shell_ordered_turn_after_an_ended_turn_starts_a_turn(self):
        """OpenCode's `!cmd` reports busy before it saves its user message."""
        self.scene([[{"text": "one"}], [{"shell": True}, {"text": "two"}]])
        handle, stream = self.start()
        stream.until(TurnEnd)
        handle.send("!ls")
        found = stream.until(TurnEnd)
        self.assertEqual(found.count(TurnStarted()), 1)
        self.assertEqual(found[-2:], [Reply("two"), TurnEnd("two")])

    def test_a_re_saved_user_message_starts_no_turn(self):
        """Each idle re-saves its turn's user messages, joined ones too."""
        self.scene([[{"join": 1}, {"text": "one"}], [{"text": "two"}]])
        handle, stream = self.start()
        handle.send("more")
        self.assertEqual(
            stream.until(TurnEnd), [TurnStarted(), Activity(""), Reply("one"), TurnEnd("one")]
        )
        stream.quiet()
        handle.send("next")
        self.assertEqual(
            stream.until(TurnEnd), [TurnStarted(), Activity(""), Reply("two"), TurnEnd("two")]
        )
        stream.quiet()

    def test_a_send_after_a_recovered_error_joins_the_turn(self):
        """The recovered error leaves the turn open: a send joins it, which then ends done."""
        error = {"name": "ContextOverflowError", "data": {"message": "compacting"}}
        self.scene([[{"error": error, "recover": True}, {"join": 1}, {"text": "answer"}]])
        handle, stream = self.start()
        handle.send("continue")
        found = stream.until(TurnEnd)
        self.assertFalse(any(isinstance(event, TurnFailed) for event in found))
        self.assertEqual(found.count(TurnStarted()), 1)
        self.assertEqual(found[-2:], [Reply("answer"), TurnEnd("answer")])

    def test_a_reply_saved_after_idle_is_still_read(self):
        self.scene([[{"text": "saved late", "late": 800}]])
        handle, stream = self.start()
        self.assertEqual(
            stream.until(TurnEnd),
            [TurnStarted(), Activity(""), Reply("saved late"), TurnEnd("saved late")],
        )
        self.assertEqual(handle.reply, "saved late")

    def test_a_late_reply_is_not_the_previous_turns(self):
        self.scene([[{"text": "first answer"}], [{"text": "second answer", "late": 800}]])
        handle, stream = self.start()
        self.assertEqual(stream.until(TurnEnd)[-1], TurnEnd("first answer"))
        handle.send("next")
        self.assertEqual(stream.until(TurnEnd)[-1], TurnEnd("second answer"))

    def test_a_turn_saved_after_idle_with_its_user_message_is_not_the_previous_turns(self):
        self.scene([[{"text": "first answer"}], [{"lateUser": 800}, {"text": "second answer"}]])
        handle, stream = self.start()
        stream.until(TurnEnd)
        handle.send("next")
        self.assertEqual(stream.until(TurnEnd)[-1], TurnEnd("second answer"))

    def test_a_tool_step_saved_before_the_late_text_does_not_end_the_wait(self):
        self.scene([[{"tool": "read"}, {"text": "after the tool", "late": 800}]])
        _, stream = self.start()
        self.assertEqual(stream.until(TurnEnd)[-1], TurnEnd("after the tool"))

    def test_an_errored_last_message_ends_the_wait_at_once(self):
        self.scene([[{"errored": True}]])
        _, stream = self.start()
        self.assertEqual(stream.until(TurnEnd)[-1], TurnEnd(interrupted=True))
        self.assertEqual(
            [row["messages_read"] for row in self.logged() if "messages_read" in row], [NATIVE]
        )

    def test_an_aborted_message_keeps_partial_reply_and_does_not_mark_the_next_turn(self):
        self.scene([[{"text": "partial"}, {"errored": True}], [{"text": "done"}]])
        handle, stream = self.start()
        found = stream.until(TurnEnd)
        self.assertIn(Reply("partial"), found)
        self.assertEqual(found[-1], TurnEnd("partial", interrupted=True))
        handle.send("continue")
        self.assertEqual(stream.until(TurnEnd)[-1], TurnEnd("done"))

    def test_a_session_abort_error_ends_as_interrupted_and_allows_a_new_turn(self):
        error = {"name": "MessageAbortedError", "data": {"message": "aborted"}}
        self.scene([[{"error": error}], [{"text": "continued"}]])
        handle, stream = self.start()
        found = stream.until(TurnEnd)
        self.assertFalse(any(isinstance(event, TurnFailed) for event in found))
        self.assertEqual(found[-1], TurnEnd(interrupted=True))
        handle.send("continue")
        self.assertEqual(stream.until(TurnEnd)[-1], TurnEnd("continued"))

    def test_a_turn_never_saved_ends_after_the_wait(self):
        self.scene([[{"unsaved": True}]])
        _, stream = self.start()
        started = time.monotonic()
        self.assertEqual(stream.until(TurnEnd)[-1], TurnEnd(""))
        self.assertGreater(time.monotonic() - started, 3)

    def blocked_turn(self, step, blocked, answer, text):
        """A turn of `step` then `text` blocks as `blocked` until `answer`, then ends done."""
        self.scene([[step, {"text": text}]])
        handle, stream = self.start()
        self.assertEqual(stream.until(Blocked)[-1], blocked)
        self.assertEqual(self.words(1)[-1], "w1 blocked")
        handle.send(answer)
        found = stream.until(TurnEnd)
        self.assertEqual((found[0], found[-1]), (Activity(""), TurnEnd(text)))
        self.assertEqual(self.words(2)[-1], "w1 done")

    def test_a_bash_ask_blocks_until_answered(self):
        self.blocked_turn({"bash": "rm -rf build"}, Blocked("bash"), "allow", "cleaned")

    def test_yolo_tools_never_report_blocked(self):
        self.session_cls = configured(self.clients, {"isolated": True})
        self.scene(
            [
                [
                    {"tool": "glob", "permission": "external_directory", "pattern": "/opt/team"},
                    {"tool": "read", "permission": "read", "pattern": "sample.env"},
                    {"bash": "git status"},
                    {"bash": "git status", "child": True},
                    {"tool": "bash", "permission": "doom_loop", "pattern": "git status"},
                    {"text": "finished"},
                ]
            ]
        )
        _, stream = self.start()
        found = stream.until(TurnEnd)
        self.assertFalse(any(isinstance(event, Blocked) for event in found), found)
        self.assertEqual(
            [event.detail for event in found if isinstance(event, Activity) and event.detail],
            ["glob", "glob", "read", "read", "bash", "bash", "bash", "bash"],
        )
        self.assertEqual(found[-1], TurnEnd("finished"))
        self.assertEqual(self.words(1), ["w1 done"])
        stream.quiet()
        self.assertEqual(self.words(1), ["w1 done"])

    def test_yolo_questions_still_block_in_root_and_child_sessions(self):
        self.session_cls = configured(self.clients, {"isolated": True})
        self.scene(
            [[{"question": "Database"}, {"question": "Region", "child": True}, {"text": "picked"}]]
        )
        handle, stream = self.start()
        for title in ("Database", "Region"):
            self.assertEqual(stream.until(Blocked)[-1], Blocked(f"question: {title}"))
            self.assertEqual(self.words(1)[-1], "w1 blocked")
            handle.send("answer")
        self.assertEqual(stream.until(TurnEnd)[-1], TurnEnd("picked"))
        self.assertEqual(self.words(3), ["w1 blocked", "w1 blocked", "w1 done"])

    def test_a_child_sessions_ask_blocks(self):
        self.blocked_turn({"bash": "make", "child": True}, Blocked("bash"), "allow", "built")

    def test_a_question_blocks_until_answered(self):
        question = {"question": "Database"}
        self.blocked_turn(question, Blocked("question: Database"), "answer", "picked")

    def test_a_dismissed_question_works_on(self):
        question = {"question": "Database", "child": True}
        self.blocked_turn(question, Blocked("question: Database"), "dismiss", "skipped")

    def test_tool_parts_are_activity_and_foreign_sessions_are_ignored(self):
        self.scene([[{"tool": "read"}, {"foreign": True}, {"busy": True}, {"text": "ok"}]])
        _, stream = self.start()
        self.assertEqual(
            stream.until(TurnEnd),
            [
                TurnStarted(),
                Activity(""),
                Activity("read"),
                Activity("read"),
                Reply("ok"),
                TurnEnd("ok"),
            ],
        )
        stream.quiet()

    def test_an_error_fails_the_turn_without_a_turn_end(self):
        error = {"name": "APIError", "data": {"message": "connection refused"}}
        self.scene([[{"error": error}], [{"text": "ok now"}]])
        handle, stream = self.start()
        self.assertEqual(stream.until(TurnFailed)[-1], TurnFailed("APIError", "connection refused"))
        stream.quiet()
        handle.send("again")
        self.assertEqual(
            stream.until(TurnEnd), [TurnStarted(), Activity(""), Reply("ok now"), TurnEnd("ok now")]
        )
        self.assertEqual(self.words(1)[-1], "w1 done")

    def test_a_compact_after_the_turn_leaves_it_done(self):
        self.scene([[{"text": "answered"}, {"compactAfter": "summary"}]])
        handle, stream = self.start()
        self.assertEqual(stream.until(TurnEnd)[-1], TurnEnd("answered"))
        stream.quiet()
        self.assertEqual(handle.status().state, StatusState.DONE)
        self.assertEqual(self.words(1), ["w1 done"])

    def test_a_recovered_error_does_not_fail_the_turn(self):
        error = {"name": "ContextOverflowError", "data": {"message": "compacting"}}
        self.scene([[{"error": error, "recover": True}, {"tool": "read"}, {"text": "done anyway"}]])
        _, stream = self.start()
        found = stream.until(TurnEnd)
        self.assertFalse(any(isinstance(event, TurnFailed) for event in found))
        self.assertEqual(found[-2:], [Reply("done anyway"), TurnEnd("done anyway")])

    def test_compacted_turns_end_done_with_their_own_replies(self):
        self.scene(
            [
                [{"compact": "summary"}, {"text": "recovered"}],
                [{"compact": "summary again"}, {"text": "next turn"}],
            ]
        )
        handle, stream = self.start()
        self.assertEqual(
            stream.until(TurnEnd),
            [TurnStarted(), Activity(""), Reply("recovered"), TurnEnd("recovered")],
        )
        stream.quiet()
        self.assertEqual(handle.status().state, StatusState.DONE)
        self.assertEqual(self.words(1), ["w1 done"])
        handle.send("again")
        self.assertEqual(
            stream.until(TurnEnd),
            [TurnStarted(), Activity(""), Reply("next turn"), TurnEnd("next turn")],
        )
        stream.quiet()
        self.assertEqual(handle.status().state, StatusState.DONE)

    def test_a_fresh_restart_rebinds_the_root_session_once(self):
        self.common.bind(str(self.cwd), "run-1", fake_opencode.NATIVE)
        _, stream = self.start()
        stream.until(TurnEnd)
        self.assertEqual(self.common.native(str(self.cwd), "run-1"), NATIVE)
        lines = RunLog(str(self.work)).read()
        bound = [line["client_id"] for line in lines if line["kind"] == "client_session"]
        self.assertEqual(bound, [NATIVE])

    def test_a_nested_opencode_finds_no_hook(self):
        self.scene([[{"nested": True}, {"text": "x"}]])
        _, stream = self.start()
        stream.until(TurnEnd)
        (call,) = self.calls()
        self.assertTrue(call["env"]["hook"])
        self.assertEqual(
            [row["nested"] for row in self.logged() if "nested" in row], [[False, False]]
        )

    def test_a_plugin_reinit_keeps_reporting(self):
        self.scene([[{"reinit": True}, {"text": "after"}], [{"text": "again"}]])
        handle, stream = self.start()
        self.assertEqual(stream.until(TurnEnd)[-1], TurnEnd("after"))
        handle.send("more")
        self.assertEqual(stream.until(TurnEnd)[-1], TurnEnd("again"))

    def test_replies_arrive_before_idle_and_skip_children_and_duplicate_updates(self):
        self.scene(
            [
                [
                    {"text": "first", "repeat": 2},
                    {"text": "child reply", "child": True},
                    {"busy": True},
                    {"reinit": True},
                    {"text": "synthetic", "synthetic": True},
                    {"text": "ignored", "ignored": True},
                    {"text": " "},
                    {"question": "Continue"},
                    {"text": "first"},
                    {"text": "last", "repeat": 2},
                ]
            ]
        )
        handle, stream = self.start()
        before = stream.until(Blocked)
        self.assertEqual([e for e in before if isinstance(e, Reply)], [Reply("first")])
        handle.send("answer")
        after = stream.until(TurnEnd)
        self.assertEqual(
            [e for e in after if isinstance(e, Reply)], [Reply("first"), Reply("last")]
        )
        self.assertEqual(after[-1], TurnEnd("last"))

    def test_each_turn_delivers_its_own_replies_even_when_text_is_repeated(self):
        self.scene([[{"text": "same"}], [{"text": "same", "late": 800}]])
        handle, stream = self.start()
        for n in range(2):
            if n:
                handle.send("next")
            self.assertEqual(
                stream.until(TurnEnd), [TurnStarted(), Activity(""), Reply("same"), TurnEnd("same")]
            )

    def test_completed_messages_are_waited_for_when_other_text_is_already_saved(self):
        self.scene(
            [
                [{"text": "early"}, {"text": "final", "reads": 6}],
                [{"text": "middle", "reads": 6}, {"text": "last"}],
            ]
        )
        handle, stream = self.start()
        self.assertEqual(
            stream.until(TurnEnd),
            [TurnStarted(), Activity(""), Reply("early"), Reply("final"), TurnEnd("final")],
        )
        handle.send("next")
        self.assertEqual(
            stream.until(TurnEnd),
            [TurnStarted(), Activity(""), Reply("middle"), Reply("last"), TurnEnd("last")],
        )

    def test_a_resume_selects_the_session_and_sends_its_prompt(self):
        self.scene([[{"text": "first"}]], resume=[[{"text": "fixed"}]])
        handle, stream = self.start()
        stream.until(TurnEnd)
        handle.stop()
        handle, stream = self.start(prompt="Fix it.", resume=True)
        self.assertEqual(stream.until(TurnEnd)[-1], TurnEnd("fixed"))
        self.assertEqual(handle.reply, "fixed")
        last = self.calls()[-1]
        self.assertEqual(last["argv"], ["--session", NATIVE, "--model", "ollama/qwen3.6:35b-mlx"])
        self.assertEqual(last["env"]["session"], NATIVE)

    def test_a_resume_of_a_run_that_bound_no_session_starts_a_fresh_one(self):
        """A run killed before OpenCode saved its session left no binding."""
        handle, stream = self.start()
        stream.until(TurnEnd)
        handle.stop()
        self.common.unbind(str(self.cwd), "run-1")
        make_db(str(self.root / "data" / "opencode" / "opencode.db"))
        self.scene([[{"text": "fresh"}]])
        handle, stream = self.start(prompt="Fix it.", resume=True)
        self.assertEqual(stream.until(TurnEnd)[-1], TurnEnd("fresh"))
        last = self.calls()[-1]
        self.assertEqual(last["argv"], ["--model", "ollama/qwen3.6:35b-mlx", "--prompt=Fix it."])
        self.assertEqual(last["env"]["session"], "")
        self.assertEqual(self.common.native(str(self.cwd), "run-1"), NATIVE)

    def test_flags_with_c_fail_before_start(self):
        with self.assertRaises(ConfigError):
            self.session_cls.start(dataclasses.replace(self.spec, prompt="x", flags=("-c",)))
        self.assertEqual(self.calls(), [])
