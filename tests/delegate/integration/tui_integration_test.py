"""A run planned and executed in tui mode on a private tmux server, set up as the session's
tui_integration_test, the session config's ClaudeClient running
tests/session/integration/clients/claude/fake_claude.py interactively (a Stop hook step: a turn end
with no prompt before it). The tui policy end to end
(FR-21, FR-23, FR-24): the nudge, the give-up, pending turn ends, a mark's reset, the idle give-up
on a clock ahead (the session's clock only), the fix send; the session left open; the launch's
/color held while the trust dialog shows, sent once it is answered. Main's
tests/integration/core/drive_tui_integration_test.py, its tui run.
"""

import io
import json
import os
import threading
import time
import unittest
from contextlib import redirect_stderr
from unittest import mock

from ensemble.delegate import Progress, Spec
from ensemble.delegate.composer import Composer
from ensemble.delegate.engine.drivers import AgentDriver
from ensemble.delegate.engine.runner import FIX, NUDGE, STOP_LIMIT
from ensemble.delegate.engine.types import Layout
from tests.delegate.case import packaged
from tests.delegate.integration.cli import DONE, SID, UNSAVED, UNSAVED_ERROR
from tests.delegate.replies import block, mark, prompt
from tests.delegate.runner import executed
from tests.delegate.unit.runs import GAVE_UP
from tests.session import live_tmux
from tests.session import tui_support as tui_live
from tests.session.integration.clients.claude import fake_claude

NAME = f"claude-{SID[:8]}"
LEFT_OPEN = f"session {NAME} left open: tmux attach -t '={NAME}'\n"
WAIT = 60  # seconds: a run
START = mark("start", "echoing")
TRUST = "Quick safety check: Is this a project you created or one you trust? 1. Yes, I trust this folder 2. No, exit"
HELD = 2  # seconds the trust dialog stays: past /color's due, 1 s after SessionStart


def stop(text: str, pending: int = 0) -> dict:
    return {
        "kind": "hook",
        "event": "Stop",
        "last_assistant_message": text,
        "background_tasks": [{}] * pending,
    }


class Ahead:
    """The session's clock: monotonic() `by` s ahead of the real clock, `by` growing `step` s per
    call.
    """

    def __init__(self):
        self.by = self.step = 0.0

    def monotonic(self) -> float:
        self.by += self.step
        return time.monotonic() + self.by


class TuiRun(tui_live.Live):
    """
    dummy-tester's echo as session SID in workdir `work`, out `out.md`, events file self.events.
    """

    def setUp(self):
        super().setUp()
        self.scenario, self.log = (
            os.path.join(self.dir, "claude.json"),
            os.path.join(self.dir, "claude.log"),
        )
        self.out = os.path.join(self.workdir, "out.md")
        self.enterContext(
            mock.patch.dict(
                os.environ,
                {
                    "CLAUDE_CONFIG_DIR": os.path.join(self.dir, "config"),
                    fake_claude.ENV: self.scenario,
                },
            )
        )
        self.driver = AgentDriver(packaged())

    def go(self, on_event=None, **scenario):
        """executed() with the fake playing `scenario`, failing past WAIT s; self.seen gets each
        callback, self.err the stderr.
        """
        with open(self.scenario, "w") as f:
            json.dump({**scenario, "log": self.log}, f)
        config, self.seen, err, ended = Composer().resolve("dummy-tester"), [], io.StringIO(), []

        def on(event):
            self.seen.append(event)
            if on_event:
                on_event(event)

        def target():
            ended.append(
                executed(
                    config,
                    Spec(
                        "Hello.",
                        self.workdir,
                        task="echo",
                        mode="tui",
                        client="claude",
                        sid=SID,
                        events=self.events,
                        out=self.out,
                    ),
                    on_event=on,
                    driver=self.driver,
                )
            )

        previous_hook = threading.excepthook

        def capture(args):
            if args.thread is thread:
                ended.append(args.exc_value)
            else:
                previous_hook(args)

        thread = threading.Thread(target=target, daemon=True)
        with redirect_stderr(err), mock.patch("threading.excepthook", new=capture):
            thread.start()
            thread.join(WAIT)
        self.err = err.getvalue()
        if not ended:
            self.fail(f"no result within {WAIT} s; stderr:\n{self.err}")
        if isinstance(ended[0], BaseException):
            raise ended[0]
        return ended[0]

    def records(self) -> list[dict]:
        with open(os.path.join(Layout(self.workdir).run, "run.jsonl")) as f:
            return [json.loads(line) for line in f]

    def kinds(self) -> list[str]:
        return [e["kind"] for e in self.records()]

    def turn_1_then(self, *rest: str) -> None:
        """run.jsonl's kinds: turn 1's, its client binding and its progress in either order (the
        transcript and the hooks are read apart), then `rest`.
        """
        kinds = self.kinds()
        self.assertEqual(sorted(kinds[2:4]), ["client_session", "progress"], kinds)
        self.assertEqual([*kinds[:2], *kinds[4:]], ["session", "input", *rest])

    def last(self, kind: str) -> dict:
        return [e for e in self.records() if e["kind"] == kind][-1]

    def users(self) -> list[str]:
        """The session's prompts: its first, then each text sent to it."""
        with open(self.last("session")["transcript"]) as f:
            found = [r["message"]["content"] for r in map(json.loads, f) if r["type"] == "user"]
        with open(self.log) as f:
            (call,) = map(json.loads, f)
        self.assertEqual(found[0], prompt(call))
        return found[1:]

    def left_open(self) -> None:
        shown = self.server.tmux(
            "display-message", "-p", "-t", f"={NAME}:", "#{session_name} #{pane_dead}"
        )
        self.assertEqual(shown.stdout, f"{NAME} 0\n", shown.stderr)


class TurnEnds(TuiRun):
    def test_the_nudges_turn_ends_done_the_deliverable_saved_the_session_left_open(self):
        result = self.go(steps=[START], turns=[[block(DONE)]])
        self.assertEqual(
            (result.returncode, result.outcome.status, result.error), (0, "done", ""), self.err
        )
        self.assertIn(f"delegate: tui session {NAME}: tmux attach -t '={NAME}'\n", self.err)
        self.assertEqual(self.users(), [NUDGE])
        self.turn_1_then("nudge", "result", "end")
        with open(self.out) as f:
            self.assertEqual(f.read(), DONE["deliverable"])
        self.left_open()
        self.lines(f"{NAME} done", f"{NAME} done")

    def test_after_the_nudge_stop_limit_turn_ends_without_an_outcome_give_up_the_session_left_open(
        self,
    ):
        result = self.go(
            steps=[START], turns=[[*[stop("Working.")] * (STOP_LIMIT - 1), "Working."]]
        )
        self.assertEqual(result, GAVE_UP)
        self.assertIn(f"delegate: no outcome after {STOP_LIMIT} turn ends; {LEFT_OPEN}", self.err)
        self.assertEqual(self.users(), [NUDGE])
        self.turn_1_then("nudge", "give_up", "result", "end")
        self.assertEqual(self.last("give_up")["reason"], f"no outcome after {STOP_LIMIT} turn ends")
        self.left_open()

    def test_turn_ends_with_work_pending_neither_nudge_nor_count(self):
        waiting = stop("Waiting for the build.", pending=1)
        result = self.go(
            steps=[waiting, waiting, START],
            turns=[[*[waiting, stop("Working.")] * (STOP_LIMIT - 1), waiting, block(DONE)]],
        )
        self.assertEqual((result.outcome.status, self.users()), ("done", [NUDGE]), self.err)

    def test_a_progress_mark_resets_the_count(self):
        half = mark("round", "half way")
        working = [stop("Working.")] * (STOP_LIMIT - 1)
        result = self.go(steps=[START], turns=[[*working, stop(half), *working[1:], block(DONE)]])
        self.assertEqual((result.outcome.status, self.users()), ("done", [NUDGE]), self.err)
        self.assertIn(Progress("round", "half way"), self.seen)


class Trust(TuiRun):
    def answer_trust(self) -> None:
        """Enter on the trust dialog HELD s after it shows."""

        def shown() -> bool:
            screen = self.server.tmux("capture-pane", "-p", "-J", "-t", f"={NAME}:").stdout
            return "Quick safety check" in screen

        live_tmux.wait(shown, WAIT, what="the trust dialog")
        time.sleep(HELD)
        self.server.tmux("send-keys", "-t", f"={NAME}:", "Enter")

    def test_color_waits_for_trust_dialog(self):
        commands = os.path.join(self.dir, "commands.jsonl")
        answer = threading.Thread(target=self.answer_trust, daemon=True)
        answer.start()
        # the fake leaves its dialog on the screen; blank lines scroll it off, as Claude clears it
        steps = [
            {"kind": "dialog", "text": TRUST},
            "\n" * 200,
            {"kind": "sleep", "seconds": HELD},
            START,
        ]
        result = self.go(steps=steps, turns=[[block(DONE)]], commands=commands)
        answer.join(WAIT)
        self.assertEqual((result.returncode, result.outcome.status), (0, "done"), self.err)
        self.assertEqual(self.users(), [NUDGE])
        self.assertEqual(
            [(c["line"], c["dialog"]) for c in map(json.loads, live_tmux.events(commands))],
            [("/color", False)],
        )


class Idle(TuiRun):
    def test_no_event_for_the_idle_timeout_gives_up_the_session_left_open(self):
        clock = Ahead()
        self.driver = AgentDriver(packaged(clock=clock.monotonic))

        def later(event):
            if isinstance(event, Progress):  # from here each clock read is past the idle timeout
                clock.step = 7200 + 1

        result = self.go(later, steps=[START], background=1)
        self.assertEqual(result, GAVE_UP)
        self.assertIn(f"delegate: no event for 2 h; {LEFT_OPEN}", self.err)
        self.turn_1_then("idle", "give_up", "result", "end")
        self.assertEqual(
            (self.last("idle")["seconds"], self.last("give_up")["reason"]),
            (7200, "no event for 2 h"),
        )
        self.assertEqual(self.users(), [])
        self.left_open()


class Fix(TuiRun):
    def test_an_invalid_outcome_is_fixed_by_one_send_to_the_session(self):
        result = self.go(steps=[f"{START}\n{block(UNSAVED)}"], turns=[[block(DONE)]])
        self.assertEqual((result.returncode, result.outcome.status), (0, "done"), self.err)
        self.assertIn(f"delegate: invalid outcome: {UNSAVED_ERROR}; resuming once\n", self.err)
        self.assertEqual(self.users(), [FIX.format(error=UNSAVED_ERROR)])
        self.turn_1_then("invalid", "result", "end")
        self.left_open()


if __name__ == "__main__":
    unittest.main()
