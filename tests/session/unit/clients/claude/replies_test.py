"""Claude mid-turn replies: queued messages joined to a turn, their answers' reply lines, the
watcher.
"""

import contextlib
import io
import json
import os
import time
from unittest import mock

from ensemble.session.clients import replies as shared
from ensemble.session.clients.claude import ClaudeClient, replies
from ensemble.session.clients.claude.tui import on_hook
from ensemble.session.engine.types import Access, HookCall, Spec
from ensemble.utils import file_lock, read_reply
from tests.session.unit.clients.claude.support import SID, Base

NAME = "w1"
HOOK = ("ensemble-session", "hook", "/x")


def said(*blocks, **extra):
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}, **extra}


def text(value):
    return {"type": "text", "text": value}


def tool(name="Bash"):
    return {"type": "tool_use", "id": "t1", "name": name, "input": {}}


PROMPT = {"type": "user", "message": {"role": "user", "content": "go"}}
RESULT = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}}
ENQUEUE = {"type": "queue-operation", "operation": "enqueue", "content": "status?"}
DEQUEUE = {"type": "queue-operation", "operation": "dequeue"}
QUEUED = {"type": "attachment", "attachment": {"type": "queued_command", "prompt": "status?"}}
REMOVE = {"type": "queue-operation", "operation": "remove"}
NOTIFIED = {  # a background task's completion, as Claude Code queues it
    "type": "attachment",
    "attachment": {
        "type": "queued_command",
        "prompt": "<task-notification>done</task-notification>",
        "commandMode": "task-notification",
    },
}


class Clock:
    """A fake monotonic clock that sleep advances."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class RepliesBase(Base):
    def setUp(self):
        super().setUp()
        self.env({"HOME": self.tmp})
        self.run = ClaudeClient({}).tui()
        spec = Spec(workdir=self.work, access=Access(cwd=self.work), sid=SID, name=NAME)
        self.path = self.run.launch(spec, HOOK).transcript
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        open(self.path, "a", encoding="utf-8").close()
        self.callback = self.run.hook_callback().state
        self.events = os.path.join(self.tmp, "events")
        open(self.events, "w", encoding="utf-8").close()
        self.spawned = self.enterContext(mock.patch.object(shared.subprocess, "Popen"))
        self.stderr = self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.state_file = replies.state_path(self.callback["store"], self.work, SID)

    def hook(self, label, *, events: str | None = "", state=None, **fields):
        payload = {"session_id": SID, "transcript_path": self.path, **fields}
        call = HookCall(
            label,
            payload,
            SID,
            self.work,
            self.events if events == "" else events,
            self.callback if state is None else state,
        )
        self.assertEqual(on_hook(call), payload)

    def append(self, *records):
        with open(self.path, "a", encoding="utf-8") as stream:
            stream.writelines(json.dumps(record) + "\n" for record in records)

    def lines(self):
        with open(self.events, encoding="utf-8") as stream:
            return [line.split()[1:] for line in stream if line.strip()]

    def answers(self):
        return [read_reply(self.events, words[2]) for words in self.lines() if words[1] == "reply"]

    def state(self):
        with open(self.state_file, encoding="utf-8") as stream:
            return json.load(stream)

    def open_turn(self):
        """Claude runs the prompt's hook, records the prompt, then calls a tool."""
        self.hook("SessionStart", source="startup")
        self.hook("UserPromptSubmit", prompt="go")
        self.append(PROMPT, said(text("working")), said(tool()))

    def stop(self, final):
        self.hook("Stop", last_assistant_message=final)

    def watch(self, sleep=None):
        clock = Clock()
        return replies.watch(self.state_file, clock=clock, sleep=sleep or clock.sleep)


class TurnTest(RepliesBase):
    def test_turn_without_interjection_writes_nothing(self):
        self.open_turn()
        self.append(RESULT)
        self.hook("PostToolUse", tool_name="Bash")
        self.append(said(text("done")))
        self.stop("done")
        self.assertEqual(self.lines(), [])
        self.spawned.assert_not_called()
        self.assertFalse(self.state()["turn"]["open"])

    def test_interjection_answer_before_more_tools_is_a_reply(self):
        self.open_turn()
        self.append(ENQUEUE, RESULT)
        self.hook("PostToolUse", tool_name="Bash")
        self.assertEqual(self.spawned.call_count, 1)
        self.append(QUEUED, said(text("MID ANSWER 6")), said(tool()))
        self.hook("PostToolUse", tool_name="Bash")
        self.assertEqual(self.answers(), ["MID ANSWER 6"])
        self.append(RESULT, said(text("SECOND DONE")))
        self.stop("SECOND DONE")
        self.assertEqual(self.answers(), ["MID ANSWER 6"])
        self.assertEqual([words[0] for words in self.lines()], [NAME])

    def test_tool_only_message_is_no_answer(self):
        self.open_turn()
        self.append(ENQUEUE, RESULT, QUEUED, said(tool()))
        self.hook("PostToolUse", tool_name="Bash")
        self.assertEqual(self.answers(), [])
        self.assertEqual(len(self.state()["turn"]["steers"]), 1)
        self.append(RESULT, said(text("answer")))
        self.stop("answer")
        self.assertEqual(self.answers(), ["answer"])

    def test_answer_only_in_stops_text(self):
        self.open_turn()
        self.append(ENQUEUE, RESULT, QUEUED)
        self.stop("both answers")
        self.assertEqual(self.answers(), ["both answers"])

    def test_each_interjection_has_its_reply(self):
        self.open_turn()
        self.append(ENQUEUE, ENQUEUE, RESULT, QUEUED, QUEUED, said(text("both")))
        self.stop("both")
        self.assertEqual(self.answers(), ["both", "both"])

    def test_task_notification_is_no_interjection(self):
        self.open_turn()
        self.append(ENQUEUE, RESULT, REMOVE, NOTIFIED, said(text("build still running")))
        self.hook("PostToolUse", tool_name="Bash")
        self.stop("build still running")
        self.assertEqual(self.lines(), [])

    def test_a_prompt_mode_message_is_an_interjection(self):
        self.open_turn()
        queued = {**QUEUED, "attachment": {**QUEUED["attachment"], "commandMode": "prompt"}}
        self.append(ENQUEUE, RESULT, queued, said(text("answer")))
        self.stop("answer")
        self.assertEqual(self.answers(), ["answer"])

    def test_queued_message_opening_a_turn_is_no_interjection(self):
        self.hook("SessionStart", source="startup")
        self.hook("UserPromptSubmit", prompt="status?")
        self.append(QUEUED, said(text("its own turn")))
        self.stop("its own turn")
        self.assertEqual(self.lines(), [])

    def test_enqueued_without_attachment_is_next_turns(self):
        self.open_turn()
        self.append(ENQUEUE, RESULT, said(text("first done")))
        self.stop("first done")
        self.assertEqual(self.lines(), [])
        self.assertEqual(self.state()["turn"]["queued"], 0)
        self.hook("UserPromptSubmit", prompt="status?")
        self.append(DEQUEUE, PROMPT, said(text("second")))
        self.stop("second")
        self.assertEqual(self.lines(), [])

    def test_a_prompt_drops_unanswered_interjections(self):
        self.open_turn()
        self.append(ENQUEUE, RESULT, QUEUED, PROMPT, said(text("new turn")))
        self.stop("new turn")
        self.assertEqual(self.lines(), [])

    def test_sidechain_records_are_skipped(self):
        self.open_turn()
        self.append(
            ENQUEUE,
            RESULT,
            QUEUED,
            said(text("subagent"), isSidechain=True),
            said(text("main answer")),
        )
        self.stop("main answer")
        self.assertEqual(self.answers(), ["main answer"])

    def test_api_error_text_is_no_answer(self):
        self.open_turn()
        self.append(ENQUEUE, RESULT, QUEUED, said(text("API Error"), isApiErrorMessage=True))
        self.append(said(text("real")))
        self.stop("real")
        self.assertEqual(self.answers(), ["real"])

    def test_prompt_mid_turn_keeps_the_turn_and_starts_a_watcher(self):
        self.open_turn()
        start = self.state()["turn"]["start"]
        self.hook("UserPromptSubmit", prompt="status?")
        self.assertEqual(self.state()["turn"]["start"], start)
        self.assertEqual(self.spawned.call_count, 1)

    def test_stale_open_turn_is_replaced(self):
        self.open_turn()
        later = time.time() + shared.LIFETIME + 1
        with mock.patch.object(replies.time, "time", return_value=later):
            self.hook("UserPromptSubmit", prompt="next")
        self.assertEqual(self.state()["turn"]["start"], os.path.getsize(self.path))
        self.spawned.assert_not_called()

    def test_stop_failure_and_session_start_close(self):
        for label in ("StopFailure", "SessionStart"):
            with self.subTest(label=label):
                self.open_turn()
                self.append(ENQUEUE, RESULT, QUEUED)
                self.hook(label, error="server_error")
                self.assertFalse(self.state()["turn"]["open"])
                self.assertEqual(self.lines(), [])

    def test_repeated_scans_give_one_reply(self):
        self.open_turn()
        self.append(ENQUEUE, RESULT, QUEUED, said(text("once")))
        for _ in range(3):
            self.hook("PostToolUse", tool_name="Bash")
        self.watch()
        self.stop("once")
        self.stop("once")
        self.assertEqual(self.answers(), ["once"])

    def test_a_restarted_turn_never_repeats_a_reply(self):
        self.open_turn()
        self.append(ENQUEUE, RESULT, QUEUED, said(text("once")))
        self.stop("once")
        self.hook("UserPromptSubmit", prompt="go")
        self.state_rewind()
        self.stop("once")
        self.assertEqual(self.answers(), ["once"])

    def state_rewind(self):
        """A lost state file: the next turn scans the same records again."""
        with file_lock(self.state_file, wait=False):
            state = self.state()
            state["turn"].update(start=0, cursor=0, spoke=False)
            with open(self.state_file, "w", encoding="utf-8") as stream:
                json.dump(state, stream)

    def test_a_partial_line_waits_for_its_end(self):
        self.open_turn()
        self.append(ENQUEUE, RESULT, QUEUED)
        with open(self.path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(said(text("half")))[:20])
        self.hook("PostToolUse", tool_name="Bash")
        self.assertEqual(self.answers(), [])
        cursor = self.state()["turn"]["cursor"]
        self.assertEqual(cursor, os.path.getsize(self.path) - 20)

    def test_a_line_longer_than_a_read_is_skipped(self):
        self.open_turn()
        self.append(ENQUEUE, RESULT, QUEUED, said(text("x" * replies.SCAN_MAX)))
        self.append(said(text("after")))
        for _ in range(3):
            self.hook("PostToolUse", tool_name="Bash")
        self.assertEqual(self.answers(), ["after"])
        self.assertEqual(self.state()["turn"]["cursor"], os.path.getsize(self.path))


class GuardTest(RepliesBase):
    def test_without_events_name_or_store_nothing_is_kept(self):
        for events, state in (
            (None, None),
            ("", {**self.callback, "name": ""}),
            ("", {key: v for key, v in self.callback.items() if key != "store"}),
            ("", {**self.callback, "store": "relative"}),
        ):
            with self.subTest(events=events, state=state):
                self.hook("UserPromptSubmit", events=events, state=state, prompt="go")
                self.assertFalse(os.path.exists(self.state_file))

    def test_a_failure_never_fails_the_hook(self):
        store = os.path.join(self.tmp, "a-file")
        open(store, "w", encoding="utf-8").close()
        self.hook("Stop", state={**self.callback, "store": store}, last_assistant_message="x")
        self.assertIn("claude reply: ", self.stderr.getvalue())

    def test_a_held_lock_times_out_within_the_deadline(self):
        self.open_turn()
        clock = Clock()
        with (
            mock.patch.object(replies.time, "monotonic", clock),
            mock.patch.object(replies.time, "sleep", clock.sleep),
            file_lock(self.state_file, wait=False),
        ):
            self.stop("done")
        self.assertLessEqual(clock.now - 1000.0, replies.DEADLINE + 0.1)
        self.assertIn("locked", self.stderr.getvalue())

    def test_a_malformed_state_file_is_fresh(self):
        self.open_turn()
        with open(self.state_file, "w", encoding="utf-8") as stream:
            stream.write(json.dumps({"version": 1, "turn": []}))
        self.stop("done")
        self.assertEqual((self.lines(), self.stderr.getvalue()), ([], ""))
        self.hook("UserPromptSubmit", prompt="next")
        self.assertTrue(self.state()["turn"]["open"])


class WatcherTest(RepliesBase):
    def test_post_tool_use_with_a_queued_message_spawns_the_watcher(self):
        self.open_turn()
        self.append(ENQUEUE)
        self.hook("PostToolUse", tool_name="Bash")
        ((args, _),) = self.spawned.call_args_list
        self.assertEqual(args[0][4:], [replies.WATCH, self.state_file])
        self.assertEqual(replies.WATCH, "ensemble.session.clients.claude.watch")

    def test_watcher_writes_reply_without_stop(self):
        self.open_turn()
        self.append(ENQUEUE, RESULT)
        self.hook("PostToolUse", tool_name="Bash")
        clock, polls = Clock(), []

        def sleep(seconds):
            polls.append(seconds)
            if len(polls) == 2:
                self.append(QUEUED, said(text("answer")))
            clock.sleep(seconds)

        self.assertEqual(replies.watch(self.state_file, clock=clock, sleep=sleep), 0)
        self.assertEqual(self.answers(), ["answer"])
        self.assertLessEqual(clock.now - 1000.0, 2 * shared.POLL + shared.IDLE_EXIT + shared.POLL)

    def test_watcher_ends_once_the_turn_closes(self):
        self.open_turn()
        self.append(ENQUEUE)
        self.hook("PostToolUse", tool_name="Bash")
        self.stop("done")
        self.assertEqual(self.watch(), 0)
