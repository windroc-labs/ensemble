"""Codex steer replies: steer detection, session file scans, reply lines and the owning harness's
settle; no hook starts a process (FR-38).
"""

import contextlib
import io
import json
import os
import time
from unittest import mock

from ensemble.session.clients.codex import tui
from ensemble.session.clients.codex.common import SessionStore
from ensemble.session.clients.codex.tui import replies
from ensemble.session.engine.types import HookCall
from ensemble.utils import STALE_AGE, file_lock, read_reply
from tests.session.case import SPAWNS
from tests.session.unit.clients.codex.support import NATIVE, Base, said
from tests.utils.clock import FakeClock

NAME = "w1"


class RepliesBase(Base):
    def setUp(self):
        super().setUp()
        self.prepare()
        self.path = self.rollout()
        self.events = str(self.root / "events")
        open(self.events, "w", encoding="utf-8").close()
        for spawn in SPAWNS:
            self.enterContext(mock.patch(spawn, side_effect=AssertionError(spawn)))
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
        self.append(said("user", prompt))

    def pump(self):
        replies.settle_pending(SessionStore().root, str(self.work), self.sid)


class SteerTest(RepliesBase):
    def test_turn_without_steer_writes_nothing(self):
        self.open_turn()
        self.append(said("assistant", "done text"))
        self.hook("Stop", turn_id="t1", last_assistant_message="done text")
        self.assertEqual(self.lines(), [])

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
        self.append(said("assistant", "working on it"))
        self.steer("status?")
        self.append(said("user", "status?"), said("assistant", "half", "done"))
        self.pump()
        (reply_id,) = self.reply_ids()
        self.assertRegex(reply_id, r"^[0-9a-f]{32}$")
        self.assertEqual(read_reply(self.events, reply_id), "half\ndone")
        self.assertEqual(self.lines()[0][0], NAME)

    def test_reply_precedes_done_and_stop_adds_no_second(self):
        self.open_turn()
        self.steer("status?")
        self.append(said("user", "status?"), said("assistant", "answer"))
        self.pump()
        self.append(said("assistant", "final"))
        self.hook("Stop", turn_id="t1", last_assistant_message="final")
        self.assertEqual(len(self.reply_ids()), 1)

    def test_stop_finds_answer_before_the_pump(self):
        self.open_turn()
        self.steer("status?")
        self.append(said("user", "status?"), said("assistant", "answer"), said("assistant", "end"))
        self.hook("Stop", turn_id="t1", last_assistant_message="end")
        (reply_id,) = self.reply_ids()
        self.assertEqual(read_reply(self.events, reply_id), "answer")
        self.assertFalse(self.state()["turn"]["open"])
        self.pump()
        self.assertEqual(len(self.reply_ids()), 1)

    def test_answers_before_the_steer_record_are_skipped(self):
        self.open_turn()
        self.steer("status?")
        self.append(
            said("assistant", "before"), said("user", "status?"), said("assistant", "after")
        )
        self.pump()
        (reply_id,) = self.reply_ids()
        self.assertEqual(read_reply(self.events, reply_id), "after")

    def test_steer_recorded_before_its_late_hook_still_replies(self):
        self.open_turn()
        self.append(said("user", "status?"), said("assistant", "answer"))
        self.steer("status?")  # the Docker Host runs a relayed hook after Codex goes on
        self.pump()
        (reply_id,) = self.reply_ids()
        self.assertEqual(read_reply(self.events, reply_id), "answer")

    def test_opening_record_is_not_a_steer_record(self):
        self.open_turn("same")
        self.append(said("assistant", "early"))
        self.steer("same")
        self.append(said("user", "same"), said("assistant", "for the steer"))
        self.pump()
        (reply_id,) = self.reply_ids()
        self.assertEqual(read_reply(self.events, reply_id), "for the steer")

    def test_failed_emit_drops_its_steer_and_still_closes(self):
        self.open_turn()
        self.steer("status?")
        self.append(said("user", "status?"), said("assistant", "answer"))
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
        self.append(said("user", "same"), said("assistant", "one"))
        self.pump()
        self.steer("same")
        self.append(said("user", "same"), said("assistant", "two"))
        self.pump()
        texts = [read_reply(self.events, reply_id) for reply_id in self.reply_ids()]
        self.assertEqual(texts, ["one", "two"])

    def test_rescan_after_restart_writes_no_second_line(self):
        self.open_turn()
        self.steer("status?")
        self.append(said("user", "status?"), said("assistant", "answer"))
        self.pump()
        saved = self.state()
        steer = {"n": 9, "prompt": "status?", "cursor": 0, "at": None, "skip": False}
        saved["steers"], saved["turn"]["claimed"] = [steer], []
        with open(self.state_file, "w", encoding="utf-8") as stream:
            json.dump(saved, stream)
        self.pump()
        self.assertEqual(len(self.reply_ids()), 1)

    def test_stop_falls_back_to_last_assistant_message(self):
        self.open_turn()
        self.steer("status?")
        self.append(said("user", "status?"))
        self.hook("Stop", turn_id="t1", last_assistant_message="the end")
        (reply_id,) = self.reply_ids()
        self.assertEqual(read_reply(self.events, reply_id), "the end")

    def test_unseen_steer_yields_nothing(self):
        self.open_turn()
        self.steer("late")
        self.append(said("assistant", "the end"))
        self.hook("Stop", turn_id="t1", last_assistant_message="the end")
        self.assertEqual(self.lines(), [])
        self.assertEqual(self.state()["steers"], [])

    def test_interrupt_scans_then_drops(self):
        self.open_turn()
        self.steer("a")
        self.steer("b")
        self.append(said("user", "a"), said("assistant", "for a"), said("user", "b"))
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

    def test_failure_keeps_hook_payload(self):
        self.open_turn()
        with mock.patch.object(os, "replace", side_effect=OSError("disk full")):
            got = self.hook("UserPromptSubmit", prompt="s", turn_id="t1")
        self.assertEqual(got["hook_event_name"], "UserPromptSubmit")
        self.assertIn("codex reply: ", self.stderr.getvalue())

    def test_non_str_payload_fields_are_absent(self):
        self.hook("SessionStart", source="startup")
        self.hook("UserPromptSubmit", prompt=5, turn_id=["x"])
        self.append(said("user", "opening"))
        self.hook("UserPromptSubmit", prompt={"a": 1}, turn_id=7)
        self.append(said("user", "anything"), said("assistant", "reply"))
        got = self.hook("Stop", last_assistant_message=[1])
        self.assertEqual(got["hook_event_name"], "Stop")
        (reply_id,) = self.reply_ids()
        self.assertEqual(read_reply(self.events, reply_id), "reply")
        self.assertNotIn("codex reply: ", self.stderr.getvalue())

    def test_lock_held_skips_within_budget(self):
        self.open_turn()
        clock = FakeClock(1000.0)
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
        self.append(said("user", long), said("assistant", "ok"))
        self.pump()
        self.assertEqual(len(self.reply_ids()), 1)

    def test_on_hook_uses_only_hook_call_paths(self):
        self.open_turn()
        self.steer("status?")
        self.append(said("user", "status?"), said("assistant", "answer"))
        self.hook("Stop", turn_id="t1", last_assistant_message="answer")
        self.assertEqual(self.lines()[0][:2], [NAME, "reply"])
        self.assertTrue(self.state_file.startswith(os.path.join(SessionStore().root, "replies")))

    def test_state_files_pruned(self):
        directory = os.path.dirname(self.state_file)
        os.makedirs(directory, exist_ok=True)
        old = os.path.join(directory, "0" * 64 + ".json")
        for name in (old, old + ".lock"):
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
        filler = said("assistant", "x" * 1000)
        self.append(*[filler] * 3000, said("user", "status?"), said("assistant", "answer"))
        state = self.state()
        start = state["turn"]["start"]
        (found,) = replies.scan(state, budget=replies.HOOK_SCAN_MAX)["steers"].values()
        self.assertIsNone(found["answer"])
        self.assertLessEqual(found["cursor"] - start, replies.SCAN_MAX)
        (found,) = replies.scan(state, budget=1000)["steers"].values()
        self.assertLessEqual(found["cursor"] - start, 1000)
        self.assertFalse(found["skip"])  # a read the budget cut short skips no record
        for _ in range(4):  # a pump's settle reads SCAN_MAX of the ~3 MB filler
            self.pump()
        (reply_id,) = self.reply_ids()
        self.assertEqual(read_reply(self.events, reply_id), "answer")

    def test_symlinked_rollout_yields_nothing(self):
        self.open_turn()
        self.steer("status?")
        other = self.root / "other.jsonl"
        other.write_text(
            json.dumps(said("user", "status?"))
            + "\n"
            + json.dumps(said("assistant", "leak"))
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


class PumpTest(RepliesBase):
    """B2: the owning harness's pump announces a steer's answer no hook follows; a steer starts no
    process (RepliesBase fails any spawn) and leaves no watch lock.
    """

    def test_a_steer_starts_no_process(self):
        self.open_turn()
        self.steer("status?")
        folder = os.path.dirname(self.state_file)
        self.assertEqual([n for n in os.listdir(folder) if n.endswith(".watch.lock")], [])

    def test_the_pump_writes_a_reply_without_a_hook(self):
        self.open_turn()
        self.steer("status?")
        self.pump()
        self.assertEqual(self.reply_ids(), [])
        self.append(said("user", "status?"), said("assistant", "answer"))
        self.pump()
        self.pump()
        self.assertEqual(len(self.reply_ids()), 1)

    def test_pump_claim_during_stop_scan_keeps_stop_commit(self):
        self.open_turn()
        self.steer("status?")
        self.append(said("user", "status?"))
        opened, rollout, ran = os.open, self.path.name, []

        def open_(name, *args, **kwargs):
            if name == rollout and not ran:  # Stop's unlocked scan: a pump's settle commits first
                ran.append(True)
                clock = FakeClock(1000.0)
                replies.settle(
                    self.state_file,
                    deadline=clock() + 1,
                    budget=replies.HOOK_SCAN_MAX,
                    clock=clock,
                    sleep=clock.sleep,
                )
                self.assertIsNotNone(self.state()["steers"][0]["at"])
                self.append(said("assistant", "answer"))
            return opened(name, *args, **kwargs)

        with mock.patch.object(os, "open", open_):
            self.hook("Stop", turn_id="t1", last_assistant_message="answer")
        self.assertTrue(ran)
        (reply_id,) = self.reply_ids()
        self.assertEqual(read_reply(self.events, reply_id), "answer")
        self.assertFalse(self.state()["turn"]["open"])
        self.assertEqual(self.state()["steers"], [])

    def test_stop_waits_behind_pump_emit(self):
        self.open_turn()
        self.steer("status?")
        self.append(said("user", "status?"), said("assistant", "answer"))
        hook_clock, link, seen = FakeClock(1000.0), os.link, []

        def claimed(*args, **kwargs):
            link(*args, **kwargs)
            if not seen:  # between the pump's claim and its line, under its lock
                seen.append(self.reply_ids())
                with (
                    mock.patch.object(replies.time, "monotonic", hook_clock),
                    mock.patch.object(replies.time, "sleep", hook_clock.sleep),
                ):
                    self.hook("Stop", turn_id="t1", last_assistant_message="answer")
                seen.append(self.reply_ids())

        with mock.patch.object(os, "link", claimed):
            self.pump()
        self.assertEqual(seen, [[], []])
        self.assertIn("locked", self.stderr.getvalue())
        self.assertEqual(len(self.reply_ids()), 1)
