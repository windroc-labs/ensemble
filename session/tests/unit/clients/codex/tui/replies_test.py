"""Codex steer replies: steer detection, session file scans, reply lines and the watcher."""

import contextlib
import io
import json
import os
import time
from unittest import mock

from ensemble.session.clients import replies as shared
from ensemble.session.clients.codex import tui
from ensemble.session.clients.codex.common import SessionStore
from ensemble.session.clients.codex.tui import replies
from ensemble.session.engine.types import HookCall
from ensemble.utils import STALE_AGE, file_lock, read_reply

from session.tests.unit.clients.codex.support import NATIVE, Base

NAME = "w1"


def _said(role, *texts):
    kind = "output_text" if role == "assistant" else "input_text"
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": role,
            "content": [{"type": kind, "text": text} for text in texts],
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
        self.prepare()
        self.path = self.rollout()
        self.events = str(self.root / "events")
        open(self.events, "w", encoding="utf-8").close()
        self.spawned = self.enterContext(mock.patch.object(shared.subprocess, "Popen"))
        self.stderr = self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.sid = str(self.spec.sid)
        self.state_file = replies.state_path(SessionStore().root, str(self.work), self.sid)

    def hook(self, label, *, events: str | None = "", name=NAME, **fields) -> dict:
        state = {"store": SessionStore().root, "name": name}
        payload = {"session_id": NATIVE, "cwd": str(self.cwd), **fields}
        call = HookCall(
            label,
            payload,
            self.sid,
            str(self.work),
            self.events if events == "" else events,
            state,
        )
        result = tui.on_hook(call)
        if result is None:
            self.fail(f"{label}: the hook journaled nothing")
        return result

    def append(self, *records):
        with self.path.open("a", encoding="utf-8") as stream:
            stream.writelines(json.dumps(record) + "\n" for record in records)

    def lines(self):
        with open(self.events, encoding="utf-8") as stream:
            return [line.split()[1:] for line in stream if line.strip()]

    def reply_ids(self):
        return [words[2] for words in self.lines() if words[1] == "reply"]

    def state(self):
        with open(self.state_file, encoding="utf-8") as stream:
            return json.load(stream)

    def steer(self, prompt="steer?", turn_id="t1"):
        self.hook("UserPromptSubmit", prompt=prompt, turn_id=turn_id)

    def open_turn(self, prompt="first", turn_id="t1"):
        """Codex runs the prompt's hook, then records the prompt."""
        self.hook("SessionStart", source="startup")
        self.hook("UserPromptSubmit", prompt=prompt, turn_id=turn_id)
        self.append(_said("user", prompt))

    def watch(self, clock=None):
        clock = clock or Clock()
        return replies.watch(self.state_file, clock=clock, sleep=clock.sleep)


class SteerTest(RepliesBase):
    def test_turn_without_steer_writes_nothing(self):
        self.open_turn()
        self.append(_said("assistant", "done text"))
        self.hook("Stop", turn_id="t1", last_assistant_message="done text")
        self.assertEqual(self.lines(), [])
        self.spawned.assert_not_called()

    def test_first_prompt_is_not_steer(self):
        self.open_turn()
        self.assertEqual(self.state()["steers"], [])
        self.assertTrue(self.state()["turn"]["open"])

    def test_new_turn_id_is_new_turn(self):
        self.open_turn(turn_id="t1")
        self.hook("UserPromptSubmit", prompt="next", turn_id="t2")
        self.assertEqual(self.state()["steers"], [])
        self.assertEqual(self.state()["turn"]["turn_id"], "t2")

    def test_stale_open_turn_is_new_turn(self):
        self.hook("SessionStart", source="startup")
        self.hook("UserPromptSubmit", prompt="first")
        later = time.time() + replies.LIFETIME + 1
        with mock.patch.object(replies.time, "time", return_value=later):
            self.hook("UserPromptSubmit", prompt="again")
        self.assertEqual(self.state()["steers"], [])

    def test_steer_then_answer_writes_one_reply_with_its_text(self):
        self.open_turn()
        self.append(_said("assistant", "working on it"))
        self.steer("status?")
        self.spawned.assert_called_once()
        self.append(_said("user", "status?"), _said("assistant", "half", "done"))
        self.assertEqual(self.watch(), 0)
        (reply_id,) = self.reply_ids()
        self.assertRegex(reply_id, r"^[0-9a-f]{32}$")
        self.assertEqual(read_reply(self.events, reply_id), "half\ndone")
        self.assertEqual(self.lines()[0][0], NAME)

    def test_reply_precedes_done_and_stop_adds_no_second(self):
        self.open_turn()
        self.steer("status?")
        self.append(_said("user", "status?"), _said("assistant", "answer"))
        self.watch()
        self.append(_said("assistant", "final"))
        self.hook("Stop", turn_id="t1", last_assistant_message="final")
        self.assertEqual(len(self.reply_ids()), 1)

    def test_stop_finds_answer_before_the_watcher(self):
        self.open_turn()
        self.steer("status?")
        self.append(
            _said("user", "status?"), _said("assistant", "answer"), _said("assistant", "end")
        )
        self.hook("Stop", turn_id="t1", last_assistant_message="end")
        (reply_id,) = self.reply_ids()
        self.assertEqual(read_reply(self.events, reply_id), "answer")
        self.assertFalse(self.state()["turn"]["open"])
        self.assertEqual(self.watch(), 0)
        self.assertEqual(len(self.reply_ids()), 1)

    def test_answers_before_the_steer_record_are_skipped(self):
        self.open_turn()
        self.steer("status?")
        self.append(
            _said("assistant", "before"), _said("user", "status?"), _said("assistant", "after")
        )
        self.watch()
        (reply_id,) = self.reply_ids()
        self.assertEqual(read_reply(self.events, reply_id), "after")

    def test_steer_recorded_before_its_late_hook_still_replies(self):
        self.open_turn()
        self.append(_said("user", "status?"), _said("assistant", "answer"))
        self.steer("status?")  # the Docker Host runs a relayed hook after Codex goes on
        self.watch()
        (reply_id,) = self.reply_ids()
        self.assertEqual(read_reply(self.events, reply_id), "answer")

    def test_opening_record_is_not_a_steer_record(self):
        self.open_turn("same")
        self.append(_said("assistant", "early"))
        self.steer("same")
        self.append(_said("user", "same"), _said("assistant", "for the steer"))
        self.watch()
        (reply_id,) = self.reply_ids()
        self.assertEqual(read_reply(self.events, reply_id), "for the steer")

    def test_failed_emit_drops_its_steer_and_still_closes(self):
        self.open_turn()
        self.steer("status?")
        self.append(_said("user", "status?"), _said("assistant", "answer"))
        with open(self.events + ".replies", "w", encoding="utf-8"):
            pass
        got = self.hook("Stop", turn_id="t1", last_assistant_message="answer")
        self.assertEqual(got["hook_event_name"], "Stop")
        self.assertEqual(self.lines(), [])
        self.assertFalse(self.state()["turn"]["open"])
        self.assertIn("codex reply: ", self.stderr.getvalue())

    def test_deadline_counts_from_the_hook_start(self):
        self.open_turn()
        call = HookCall(
            "Interrupt",
            {"session_id": NATIVE, "cwd": str(self.cwd)},
            self.sid,
            str(self.work),
            self.events,
            {"store": SessionStore().root, "name": NAME},
        )
        with file_lock(self.state_file, wait=False):
            began = time.monotonic()
            replies.on_hook(call, began=began - replies.DEADLINES["Interrupt"])
            self.assertLess(time.monotonic() - began, 0.5)
        self.assertIn("locked", self.stderr.getvalue())

    def test_identical_prompts_match_in_order(self):
        self.open_turn()
        self.steer("same")
        self.append(_said("user", "same"), _said("assistant", "one"))
        self.watch()
        self.steer("same")
        self.append(_said("user", "same"), _said("assistant", "two"))
        self.watch()
        texts = [read_reply(self.events, reply_id) for reply_id in self.reply_ids()]
        self.assertEqual(texts, ["one", "two"])

    def test_rescan_after_restart_writes_no_second_line(self):
        self.open_turn()
        self.steer("status?")
        self.append(_said("user", "status?"), _said("assistant", "answer"))
        self.watch()
        saved = self.state()
        steer = {"n": 9, "prompt": "status?", "cursor": 0, "at": None, "skip": False}
        saved["steers"], saved["turn"]["claimed"] = [steer], []
        with open(self.state_file, "w", encoding="utf-8") as stream:
            json.dump(saved, stream)
        self.watch()
        self.assertEqual(len(self.reply_ids()), 1)

    def test_stop_falls_back_to_last_assistant_message(self):
        self.open_turn()
        self.steer("status?")
        self.append(_said("user", "status?"))
        self.hook("Stop", turn_id="t1", last_assistant_message="the end")
        (reply_id,) = self.reply_ids()
        self.assertEqual(read_reply(self.events, reply_id), "the end")

    def test_unseen_steer_yields_nothing(self):
        self.open_turn()
        self.steer("late")
        self.append(_said("assistant", "the end"))
        self.hook("Stop", turn_id="t1", last_assistant_message="the end")
        self.assertEqual(self.lines(), [])
        self.assertEqual(self.state()["steers"], [])

    def test_interrupt_scans_then_drops(self):
        self.open_turn()
        self.steer("a")
        self.steer("b")
        self.append(_said("user", "a"), _said("assistant", "for a"), _said("user", "b"))
        self.hook("Interrupt", turn_id="t1", last_assistant_message="ignored")
        texts = [read_reply(self.events, reply_id) for reply_id in self.reply_ids()]
        self.assertEqual(texts, ["for a"])
        self.assertEqual(self.state()["steers"], [])
        self.assertFalse(self.state()["turn"]["open"])

    def test_session_start_resets(self):
        self.open_turn()
        self.steer("status?")
        self.hook("SessionStart", source="resume")
        self.assertEqual(self.state()["steers"], [])
        self.assertFalse(self.state()["turn"]["open"])

    def test_no_events_or_name_no_state(self):
        self.hook("SessionStart", source="startup", events=None)
        self.hook("UserPromptSubmit", prompt="x", events=None)
        self.hook("UserPromptSubmit", prompt="y", name="")
        self.assertFalse(os.path.lexists(self.state_file))
        self.spawned.assert_not_called()

    def test_failure_keeps_hook_payload(self):
        self.open_turn()
        with mock.patch.object(os, "replace", side_effect=OSError("disk full")):
            got = self.hook("UserPromptSubmit", prompt="s", turn_id="t1")
        self.assertEqual(got["hook_event_name"], "UserPromptSubmit")
        self.assertIn("codex reply: ", self.stderr.getvalue())

    def test_non_str_payload_fields_are_absent(self):
        self.hook("SessionStart", source="startup")
        self.hook("UserPromptSubmit", prompt=5, turn_id=["x"])
        self.append(_said("user", "opening"))
        self.hook("UserPromptSubmit", prompt={"a": 1}, turn_id=7)
        self.append(_said("user", "anything"), _said("assistant", "reply"))
        got = self.hook("Stop", last_assistant_message=[1])
        self.assertEqual(got["hook_event_name"], "Stop")
        (reply_id,) = self.reply_ids()
        self.assertEqual(read_reply(self.events, reply_id), "reply")
        self.assertNotIn("codex reply: ", self.stderr.getvalue())

    def test_lock_held_skips_within_budget(self):
        self.open_turn()
        clock = Clock()
        with (
            mock.patch.object(replies.time, "monotonic", clock),
            mock.patch.object(replies.time, "sleep", clock.sleep),
            file_lock(self.state_file, wait=False),
        ):
            got = self.hook("Interrupt", turn_id="t1")
        self.assertEqual(got["hook_event_name"], "Interrupt")
        self.assertLessEqual(clock.now - 1000.0, replies.DEADLINES["Interrupt"] + 0.1)
        self.assertIn("codex reply: ", self.stderr.getvalue())

    def test_pending_cap(self):
        self.open_turn()
        for index in range(replies.PENDING_MAX + 1):
            self.steer(f"s{index}")
        prompts = [steer["prompt"] for steer in self.state()["steers"]]
        self.assertEqual(prompts, [f"s{index}" for index in range(1, replies.PENDING_MAX + 1)])

    def test_long_prompt_matches_by_prefix(self):
        self.open_turn()
        long = "word " * replies.PROMPT_MAX
        self.steer(long)
        self.append(_said("user", long), _said("assistant", "ok"))
        self.watch()
        self.assertEqual(len(self.reply_ids()), 1)

    def test_on_hook_uses_only_hook_call_paths(self):
        self.open_turn()
        self.steer("status?")
        self.append(_said("user", "status?"), _said("assistant", "answer"))
        self.hook("Stop", turn_id="t1", last_assistant_message="answer")
        self.assertEqual(self.lines()[0][:2], [NAME, "reply"])
        self.assertTrue(self.state_file.startswith(os.path.join(SessionStore().root, "replies")))

    def test_state_files_pruned(self):
        directory = os.path.dirname(self.state_file)
        os.makedirs(directory, exist_ok=True)
        old = os.path.join(directory, "0" * 64 + ".json")
        for name in (old, old + ".lock", old + ".watch.lock"):
            with open(name, "w", encoding="utf-8") as stream:
                stream.write("{}")
            then = time.time() - STALE_AGE - 10
            os.utime(name, (then, then))
        self.open_turn()
        self.assertEqual(
            sorted(os.listdir(directory)),
            sorted(os.path.basename(p) for p in (self.state_file, self.state_file + ".lock")),
        )


class ScanTest(RepliesBase):
    def test_scan_reads_at_most_scan_max(self):
        self.open_turn()
        self.steer("status?")
        filler = _said("assistant", "x" * 1000)
        self.append(*[filler] * 3000, _said("user", "status?"), _said("assistant", "answer"))
        state = self.state()
        start = state["turn"]["start"]
        (found,) = replies.scan(state, budget=replies.HOOK_SCAN_MAX)["steers"].values()
        self.assertIsNone(found["answer"])
        self.assertLessEqual(found["cursor"] - start, replies.SCAN_MAX)
        (found,) = replies.scan(state, budget=1000)["steers"].values()
        self.assertLessEqual(found["cursor"] - start, 1000)
        self.assertFalse(found["skip"])  # a read the budget cut short skips no record
        self.watch()
        (reply_id,) = self.reply_ids()
        self.assertEqual(read_reply(self.events, reply_id), "answer")

    def test_symlinked_rollout_yields_nothing(self):
        self.open_turn()
        self.steer("status?")
        other = self.root / "other.jsonl"
        other.write_text(
            json.dumps(_said("user", "status?"))
            + "\n"
            + json.dumps(_said("assistant", "leak"))
            + "\n",
            encoding="utf-8",
        )
        self.path.unlink()
        self.path.symlink_to(other)
        self.hook("Stop", turn_id="t1", last_assistant_message="end")
        self.assertEqual(self.lines(), [])

    def test_fifo_rollout_does_not_block(self):
        self.open_turn()
        self.steer("status?")
        self.path.unlink()
        os.mkfifo(self.path)
        got = self.hook("Stop", turn_id="t1", last_assistant_message="end")
        self.assertEqual(got["hook_event_name"], "Stop")
        self.assertEqual(self.lines(), [])


class WatcherTest(RepliesBase):
    def test_ensure_watcher_spawns_detached(self):
        self.open_turn()
        self.steer("status?")
        ((args, kwargs),) = self.spawned.call_args_list
        self.assertEqual(args[0][1:5], ["-P", "-s", "-m", replies.WATCH])
        self.assertEqual(args[0][5], self.state_file)
        self.assertTrue(kwargs["start_new_session"])
        for stream in ("stdin", "stdout", "stderr"):
            self.assertEqual(kwargs[stream], shared.subprocess.DEVNULL)
        self.assertEqual(replies.WATCH, "ensemble.session.clients.codex.tui.watch")

    def test_no_spawn_while_a_watcher_holds_its_lock(self):
        self.open_turn()
        with file_lock(self.state_file + ".watch", wait=False):
            self.steer("status?")
        self.spawned.assert_not_called()

    def test_second_watcher_exits(self):
        self.open_turn()
        self.steer("status?")
        with file_lock(self.state_file + ".watch", wait=False):
            clock = Clock()
            self.assertEqual(replies.watch(self.state_file, clock=clock, sleep=clock.sleep), 0)
            self.assertEqual(clock.now, 1000.0)

    def test_watcher_writes_reply_without_stop(self):
        self.open_turn()
        self.steer("status?")
        clock = Clock()
        polls = []

        def sleep(seconds):
            polls.append(seconds)
            if len(polls) == 3:
                self.append(_said("user", "status?"), _said("assistant", "answer"))
            clock.sleep(seconds)

        self.assertEqual(replies.watch(self.state_file, clock=clock, sleep=sleep), 0)
        self.assertEqual(len(self.reply_ids()), 1)
        self.assertLessEqual(clock.now - 1000.0, 3 * shared.POLL + shared.IDLE_EXIT + shared.POLL)

    def test_watcher_idle_quiet_and_lifetime_exit(self):
        self.open_turn()
        clock = Clock()
        self.assertEqual(self.watch(clock), 0)
        self.assertLessEqual(clock.now - 1000.0, shared.IDLE_EXIT + shared.POLL)
        self.steer("never answered")
        clock = Clock()
        self.assertEqual(self.watch(clock), 0)
        self.assertGreaterEqual(clock.now - 1000.0, shared.QUIET)
        self.assertLess(clock.now - 1000.0, shared.QUIET + 2 * shared.POLL)

        def growing(_):
            self.append({"type": "event_msg", "payload": {}})
            clock.sleep(600.0)  # long polls keep this loop short

        clock = Clock()
        self.assertEqual(replies.watch(self.state_file, clock=clock, sleep=growing), 0)
        self.assertGreaterEqual(clock.now - 1000.0, replies.LIFETIME)

    def test_watcher_claim_during_stop_scan_keeps_stop_commit(self):
        self.open_turn()
        self.steer("status?")
        self.append(_said("user", "status?"))
        opened, rollout, ran = os.open, self.path.name, []

        def open_(name, *args, **kwargs):
            if name == rollout and not ran:  # Stop's unlocked scan: a watcher poll commits first
                ran.append(True)
                clock = Clock()
                replies.settle(
                    self.state_file,
                    deadline=clock() + 1,
                    budget=replies.HOOK_SCAN_MAX,
                    clock=clock,
                    sleep=clock.sleep,
                )
                self.assertIsNotNone(self.state()["steers"][0]["at"])
                self.append(_said("assistant", "answer"))
            return opened(name, *args, **kwargs)

        with mock.patch.object(os, "open", open_):
            self.hook("Stop", turn_id="t1", last_assistant_message="answer")
        self.assertTrue(ran)
        (reply_id,) = self.reply_ids()
        self.assertEqual(read_reply(self.events, reply_id), "answer")
        self.assertFalse(self.state()["turn"]["open"])
        self.assertEqual(self.state()["steers"], [])

    def test_stop_waits_behind_watcher_emit(self):
        self.open_turn()
        self.steer("status?")
        self.append(_said("user", "status?"), _said("assistant", "answer"))
        hook_clock, link, seen = Clock(), os.link, []

        def claimed(*args, **kwargs):
            link(*args, **kwargs)
            if not seen:  # between the watcher's claim and its line, under its lock
                seen.append(self.reply_ids())
                with (
                    mock.patch.object(replies.time, "monotonic", hook_clock),
                    mock.patch.object(replies.time, "sleep", hook_clock.sleep),
                ):
                    self.hook("Stop", turn_id="t1", last_assistant_message="answer")
                seen.append(self.reply_ids())

        with mock.patch.object(os, "link", claimed):
            self.watch()
        self.assertEqual(seen, [[], []])
        self.assertIn("locked", self.stderr.getvalue())
        self.assertEqual(len(self.reply_ids()), 1)
