"""HostedSession's tui mode with the real ClaudeClient running fake_claude.py on a private tmux
server.
"""

import functools
import json
import os
import shlex
import signal
import subprocess
import unittest
from datetime import UTC, datetime
from unittest import mock

from ensemble.session import Session
from ensemble.session.clients.claude.tui import ClaudeTui
from ensemble.session.engine.records import RunLog
from ensemble.session.engine.types import (
    Access,
    Activity,
    Blocked,
    Exited,
    IdleTimeout,
    LimitHit,
    Mode,
    Reply,
    SessionError,
    Spec,
    TurnEnd,
    TurnFailed,
    TurnStarted,
)
from ensemble.session.modes import tui
from tests.session import live_tmux
from tests.session import tui_support as tui_live
from tests.session.case import PY
from tests.session.cli_support import guard_command
from tests.session.integration.clients.claude import fake_claude

MANAGER, SID, IDLE, WAIT, Stream = (
    tui_live.MANAGER,
    tui_live.SID,
    tui_live.IDLE,
    tui_live.WAIT,
    tui_live.Stream,
)
TABLE = {
    "implementation": "ensemble.session.clients.claude:ClaudeClient",
}
TRUST = "Quick safety check: Is this a project you created or one you trust?\n1. Yes, I trust this folder\n2. No, exit"
LIMIT = "You've hit your weekly limit · resets Oct 15, 2027, 9am (UTC)"
RESETS = datetime(2027, 10, 15, 9, tzinfo=UTC).timestamp()
# Another process attaching by name, sending text: prints [sid, alive, reply, TurnEnd, reply]
ATTACH = """import json, sys
table, name, text = sys.argv[1:]
from ensemble.session import Host, Session, TurnEnd
Session.configure(clients={"claude": json.loads(table)}, hosts=Host.parse_hosts({"default": "tmux", "tmux": {"implementation": "ensemble.session.hosts.tmux:TmuxHost"}}))
handle = Session.attach(name)
before = [handle.sid, handle.alive(), handle.reply]
handle.send(text)
event = next(e for e in handle.events() if isinstance(e, TurnEnd))
print(json.dumps([*before, repr(event), handle.reply]))
"""


class HookStream(Stream):
    """A Stream of the hook and screen events only: Replies' timing against them varies."""

    def _drain(self, handle):
        for event in handle.events():
            if not isinstance(event, Reply):
                self.queue.put(event)


class Live(tui_live.Live):
    def setUp(self):
        super().setUp()
        self.scenario = os.path.join(self.dir, "claude.json")
        self.log = os.path.join(self.dir, "claude.log")
        self.command_log = os.path.join(self.dir, "commands.jsonl")
        patch = mock.patch.dict(
            os.environ,
            {"CLAUDE_CONFIG_DIR": os.path.join(self.dir, "config"), fake_claude.ENV: self.scenario},
        )
        patch.start()
        self.addCleanup(patch.stop)
        self.configure()

    def configure(self, **table) -> None:
        self.configure_session({"claude": {**TABLE, **table}})

    def start(
        self,
        name: str,
        prompt: str = "hello",
        *,
        scenario: dict,
        sid=None,
        resume=False,
        **kwargs,
    ) -> Session:
        with open(self.scenario, "w", encoding="utf-8") as f:
            json.dump({"log": self.log, "commands": self.command_log, **scenario}, f)
        handle = self.session_cls.start(
            Spec(
                workdir=self.workdir,
                access=Access(cwd=self.workdir),
                name=name,
                sid=sid,
                client="claude",
                mode=Mode.TUI,
                prompt=prompt,
                resume=resume,
                events=self.events,
            ),
            **kwargs,
        )
        self.addCleanup(handle.stop)
        return handle

    def show(self, name: str) -> str | None:
        host = self.session_cls.host()
        if not host.attachable:
            self.fail(f"host {host.name} cannot show")
        return self.session_cls.viewer().show(name, host)

    def stream(self, handle: Session, *, replies: bool = False) -> Stream:
        """Its handle stops at cleanup, so its thread never polls a later test's server."""
        stream = (Stream if replies else HookStream)(self, handle)
        self.addCleanup(handle.stop)
        return stream

    def calls(self) -> list[dict]:
        with open(self.log, encoding="utf-8") as f:
            return [json.loads(line) for line in f]

    def colored(self) -> list[dict]:
        """The local commands the fake read, once it read one."""
        return live_tmux.wait(
            lambda: [json.loads(line) for line in live_tmux.events(self.command_log)],
            what="the launch's /color",
        )

    def users(self, transcript: str) -> list[str]:
        with open(transcript, encoding="utf-8") as f:
            return [r["message"]["content"] for r in map(json.loads, f) if r["type"] == "user"]

    def panes(self) -> int:
        return len(self.server.panes(f"={MANAGER}:"))

    def typed(self, handle: Session, *keys: str) -> None:
        """keys typed into a pane of the manager's window running handle's attach command, as a
        user's terminal would, once it shows the worker's screen.
        """
        res = self.server.tmux(
            *("split-window", "-d", "-P", "-F", "#{pane_id}", "-t", f"={MANAGER}:", "--"),
            *shlex.split(handle.attach_command()),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        pane = res.stdout.strip()
        live_tmux.wait(
            lambda: self.server.tmux("capture-pane", "-p", "-t", pane).stdout.strip(),
            what=f"{handle.name} shown in {pane}",
        )
        res = self.server.tmux("send-keys", "-t", pane, *keys)
        self.assertEqual(res.returncode, 0, res.stderr)

    def closed(self, handle: Session) -> None:
        self.assertEqual((handle.alive(), self.session_cls.list(handle.name)), (False, []))
        self.assertFalse(
            os.path.lexists(tui.TuiStore.attach(handle.name, host=self.session_cls.host()).dir)
        )


class TurnsTest(Live):
    def test_foreign_stop_cannot_mark_the_active_session_done(self):
        handle = self.start(
            "w1",
            sid=SID,
            scenario={
                "steps": ["old reply"],
                "turns": [[{"kind": "dialog", "text": "Waiting after prompt"}]],
            },
        )
        stream = self.stream(handle)
        self.assertEqual(stream.next(idle=False), TurnStarted())
        self.assertEqual(stream.next(idle=False), TurnEnd("old reply", 0))
        self.colored()
        handle.send("continue")
        live_tmux.wait(
            lambda: "Waiting after prompt" in handle.screen(), what="second turn waiting"
        )
        live_tmux.wait(lambda: live_tmux.activity("w1") == "working", what="w1 marked working")
        before = live_tmux.events(self.events)
        store = tui.TuiStore.attach("w1", host=self.session_cls.host())
        hooks = live_tmux.events(os.path.join(store.dir, "hooks.jsonl"))
        result = subprocess.run(
            (*store.hook_argv(), "Stop"),
            input=json.dumps(
                {
                    "session_id": "00000000-0000-4000-8000-000000000002",
                    "last_assistant_message": "foreign",
                }
            ),
            text=True,
            capture_output=True,
            env=self.server.child_env(),
            cwd=self.workdir,
            timeout=WAIT,
        )
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(live_tmux.activity("w1"), "working")
        self.assertEqual(live_tmux.events(self.events), before)
        self.assertEqual(live_tmux.events(os.path.join(store.dir, "hooks.jsonl")), hooks)
        self.assertIn("Waiting after prompt", handle.content())

    def test_turns_their_lines_state_and_reply_send_and_the_border_no_pane_opened(self):
        handle = self.start(
            "w1",
            scenario={"steps": ["Hi from w1."], "turns": [["Second."]], "background": 2},
            sid=SID,
            idle_timeout=IDLE,
        )
        self.assertEqual((self.panes(), self.server.pane_of("w1")), (1, ""))
        stream = self.stream(handle)
        self.assertEqual(
            [stream.next(idle=False) for _ in range(2)],
            [TurnStarted(), TurnEnd("Hi from w1.", 2)],
        )
        self.assertEqual(stream.next(), IdleTimeout(IDLE))
        argv = self.calls()[0]["argv"]
        self.assertEqual((argv[argv.index("--session-id") + 1], argv[-2:]), (SID, ["--", "hello"]))
        hooks = json.loads(argv[argv.index("--settings") + 1])["hooks"]
        hook = tui.TuiStore.attach("w1", host=self.session_cls.host()).hook_argv()
        self.assertEqual(
            {label: entries[0]["hooks"][0]["command"] for label, entries in hooks.items()},
            {label: shlex.join((*hook, label)) for label in (*ClaudeTui.hooks, "SessionStart")},
        )
        self.lines("w1 done")
        self.assertEqual((live_tmux.activity("w1"), handle.reply), ("done", "Hi from w1."))

        self.colored()
        handle.send("more")
        self.assertEqual(
            [stream.next(idle=False) for _ in range(2)], [TurnStarted(), TurnEnd("Second.", 2)]
        )
        self.lines("w1 done", "w1 done")
        self.assertEqual((live_tmux.activity("w1"), handle.reply), ("done", "Second."))
        self.assertEqual(self.users(handle.info().transcript), ["hello", "more"])
        window = f"={MANAGER}:"  # no pane opened: no label set anywhere
        for option in ("pane-border-status", "pane-border-format"):
            found = self.server.tmux("show-options", "-wqv", "-t", window, option)
            self.assertEqual((found.returncode, found.stdout), (0, ""), found.stderr)
        self.assertEqual((self.server.sessions(), self.server.pane_of("w1")), ([MANAGER], ""))
        self.assertEqual(
            (handle.alive(), [r.name for r in self.session_cls.list("w1")], self.panes()),
            (True, ["w1"], 1),
        )


class MidTurnTest(Live):
    """Input Claude joins to its running turn (Claude Code 2.1.312): a send while its tool runs, or
    a background notice."""

    def test_a_send_mid_turn_joins_it(self):
        steps = [{"kind": "queued", "answer": "Noted.", "prompted": True}, "Done."]
        handle = self.start("w1", scenario={"steps": steps})
        stream = self.stream(handle)
        self.assertEqual(stream.next(idle=False), TurnStarted())
        transcript = handle.info().transcript

        def tool_runs():  # its hook runs before Claude writes the transcript
            return any('"tool_use"' in line for line in live_tmux.events(transcript))

        live_tmux.wait(tool_runs, what="the turn's tool")
        handle.send("also this")
        self.assertEqual(
            [stream.next(idle=False) for _ in range(2)], [Activity("Bash"), TurnEnd("Done.", 0)]
        )
        stream.quiet(1.0)

        def done():
            self.poke()
            found = [line.split()[1:3] for line in live_tmux.events(self.events)]
            return found if found[-1:] == [["w1", "done"]] else None

        self.assertEqual(
            live_tmux.wait(done, what="w1's done line"), [["w1", "reply"], ["w1", "done"]]
        )

    def test_background_notices_mid_turn_join_it_no_send(self):
        """Each of a council's background agents ends with a notice: each joins."""
        steps = [
            "Waiting.",
            {"kind": "tool", "name": "Agent"},
            *(
                {"kind": "notice", "text": f"<task-notification>agent {n} done</task-notification>"}
                for n in range(3)
            ),
            "Done.",
        ]
        handle = self.start("w1", scenario={"steps": steps})
        stream = self.stream(handle)
        self.assertEqual(
            [stream.next(idle=False) for _ in range(3)],
            [TurnStarted(), Activity("Agent"), TurnEnd("Done.", 0)],
        )
        stream.quiet(1.0)
        self.lines("w1 done")


def delivered(events: list) -> list[str]:
    """Each Reply's text, then its TurnEnd's unless that repeats the last."""
    texts = [e.text for e in events if isinstance(e, Reply)]
    end = events[-1].reply
    return [*texts, end] if end and texts[-1:] != [end] else texts


class RepliesTest(Live):
    def test_each_text_of_a_turn_is_delivered_once(self):
        steps = ["Start: go.", {"kind": "tool", "name": "Bash"}, "Middle.", "Done."]
        handle = self.start("w1", scenario={"steps": steps})
        events = self.stream(handle, replies=True).until(TurnEnd, idle=False)
        self.assertEqual(delivered(events), ["Start: go.", "Middle.", "Done."])
        self.assertIn(Activity("Bash"), events)

    def test_prompted_launch_colors_without_extra_turn(self):
        handle = self.start("w1", scenario={"steps": ["Hi."]})
        stream = self.stream(handle, replies=True)
        events = stream.until(TurnEnd, idle=False)
        self.assertEqual((delivered(events), events[-1]), (["Hi."], TurnEnd("Hi.", 0)))
        (call,) = self.calls()
        self.assertEqual(
            [(c["pid"], c["line"], c["dialog"]) for c in self.colored()],
            [(call["pid"], "/color", False)],
        )
        stream.quiet(1.0)
        self.assertEqual(self.users(handle.info().transcript), ["hello"])


class BlockedTest(Live):
    def test_a_permission_request_is_blocked_holding_the_idle_timer_till_answered(self):
        steps = [
            {"kind": "hook", "event": "PermissionRequest", "tool_name": "Edit"},
            {"kind": "dialog", "text": "Allow this edit? 1. Yes 2. No"},
            "Edited.",
        ]
        handle = self.start("w1", scenario={"steps": steps}, idle_timeout=IDLE)
        stream = self.stream(handle)
        self.assertEqual(
            [stream.next(idle=False) for _ in range(2)],
            [TurnStarted(), Blocked("Edit")],
        )
        self.lines("w1 blocked")
        self.assertEqual(live_tmux.activity("w1"), "blocked")
        live_tmux.wait(
            lambda: "Allow this edit?" in handle.screen(), what="the dialog on the screen"
        )
        stream.quiet(3 * IDLE)
        handle.send("1")
        self.assertEqual(stream.next(), TurnEnd("Edited.", 0))
        self.assertEqual(stream.next(), IdleTimeout(IDLE))
        self.lines("w1 blocked", "w1 done")

    def test_the_trust_dialog_on_the_screen_is_blocked_and_marked(self):
        handle = self.start(
            "w1", scenario={"steps": [{"kind": "dialog", "text": TRUST}, "Trusted."]}
        )
        stream = self.stream(handle)
        self.assertEqual(
            [stream.next() for _ in range(2)],
            [TurnStarted(), Blocked("Quick safety check")],
        )
        self.lines("w1 blocked")
        self.assertEqual(live_tmux.activity("w1"), "blocked")
        self.typed(handle, "Enter")
        self.assertEqual(stream.next(), TurnEnd("Trusted.", 0))
        self.lines("w1 blocked", "w1 done")


class LimitTest(Live):
    def test_the_usage_limit_is_one_limit_hit_the_session_waiting(self):
        handle = self.start(
            "w1", scenario={"steps": [{"kind": "limit", "text": LIMIT}], "turns": [["Back."]]}
        )
        stream = self.stream(handle)
        # the limit record can be read with the prompt's hook record, and is put first
        self.assertCountEqual([stream.next() for _ in range(2)], [TurnStarted(), LimitHit(RESETS)])
        stream.quiet(1.0)
        self.lines("w1 done")
        self.assertEqual((handle.alive(), live_tmux.activity("w1")), (True, "done"))
        handle.send("go on")  # the limit ended no turn: the launch's /color waits for this one
        self.assertEqual(  # its harness fails the open limit turn as the new one starts
            [stream.next() for _ in range(3)],
            [TurnFailed("superseded"), TurnStarted(), TurnEnd("Back.", 0)],
        )
        self.lines("w1 done", "w1 done")
        self.colored()
        self.assertIn("You've hit your weekly limit", handle.screen())
        stream.quiet(1.0)

    def test_limit_text_the_agent_quotes_is_no_limit(self):
        handle = self.start("w1", scenario={"steps": [f"Docs example: {LIMIT}"]})
        stream = self.stream(handle)
        self.assertEqual(
            [stream.next() for _ in range(2)],
            [TurnStarted(), TurnEnd(f"Docs example: {LIMIT}", 0)],
        )
        live_tmux.wait(lambda: LIMIT in " ".join(handle.screen().split()), what="the quote")
        stream.quiet(1.0)


class DeadTest(Live):
    def test_a_sigterm_ends_with_its_signal_and_a_dead_line_leaving_inventory(self):
        handle = self.start("w1", scenario={"steps": ["Hi."]})
        stream = self.stream(handle)
        self.assertEqual(
            [stream.next() for _ in range(2)],
            [TurnStarted(), TurnEnd("Hi.", 0)],
        )
        self.lines("w1 done")  # the Stop hook ended: the pane's death would hang it up
        os.kill(self.calls()[0]["pid"], signal.SIGTERM)
        self.assertEqual(stream.next(poke=self.poke), Exited(-signal.SIGTERM))
        self.lines("w1 done", "w1 dead")
        self.assertEqual(self.server.sessions(), [MANAGER])
        live_tmux.wait(
            lambda: not self.session_cls.list("w1"), what="exited guard leaves inventory"
        )
        self.assertEqual((handle.alive(), self.session_cls.list("w1")), (False, []))
        for call in (lambda: handle.send("more"), handle.screen):
            with self.assertRaises(SessionError):
                call()
        tui.TuiStore.attach("w1", host=self.session_cls.host()).close()
        self.closed(handle)

    def test_an_early_death_ends_with_its_exit_code_and_a_dead_line_its_screen_shown(self):
        """A client dead at once lingers in its guard: a show right after start shows its last
        screen.
        """
        self.server.attach(MANAGER)
        self.server.tmux("set-option", "-w", "-t", f"={MANAGER}:", "remain-on-exit", "on")
        self.configure(flags=["--bogus"])
        handle = self.start("w2", scenario={})
        with mock.patch.dict(os.environ, self.server.inside(MANAGER)):
            self.assertIsNone(self.show("w2"))
        (pane,) = self.server.worker_panes("w2", dead=True)
        live_tmux.wait(
            lambda: (
                "claude: error: unrecognized arguments: --bogus"
                in self.server.tmux("capture-pane", "-p", "-J", "-t", pane.pane).stdout
            ),
            what="w2's last screen in its worker pane",
        )
        self.assertEqual(self.stream(handle).next(poke=self.poke), Exited(2))
        self.lines("w2 dead")
        self.assertEqual(self.server.sessions(), [MANAGER])
        live_tmux.wait(
            lambda: not self.session_cls.list("w2"), what="exited guard leaves inventory"
        )
        self.assertEqual(self.session_cls.list("w2"), [])
        with self.assertRaises(SessionError):
            handle.screen()
        tui.TuiStore.attach("w2", host=self.session_cls.host()).close()
        self.closed(handle)


class ResumeTest(Live):
    def test_a_resume_continues_the_session_and_its_transcript(self):
        first = self.start("w1", scenario={"steps": ["One."]}, sid=SID)
        stream = self.stream(first)
        self.assertEqual([stream.next() for _ in range(2)], [TurnStarted(), TurnEnd("One.", 0)])
        transcript = first.info().transcript
        self.session_cls.retire("w1")
        self.assertEqual(stream.until_exited(), [Exited(-signal.SIGHUP)])
        again = self.start("w1", "again", scenario={"steps": ["Two."]}, sid=SID, resume=True)
        events = self.stream(again, replies=True).until(TurnEnd, idle=False)
        self.assertEqual(
            ([e for e in events if not isinstance(e, Reply)], delivered(events)),
            ([TurnStarted(), TurnEnd("Two.", 0)], ["Two."]),
        )
        argv = self.calls()[1]["argv"]
        self.assertEqual((argv[argv.index("--resume") + 1], "--session-id" in argv), (SID, False))
        self.assertEqual(again.info().transcript, transcript)
        self.assertEqual(self.users(again.info().transcript), ["hello", "again"])


class AttachTest(Live):
    def test_another_process_attaches_by_name_reads_the_reply_and_sends(self):
        handle = self.start("w1", scenario={"steps": ["One."], "turns": [["Two."]]}, sid=SID)
        stream = self.stream(handle)
        self.assertEqual([stream.next() for _ in range(2)], [TurnStarted(), TurnEnd("One.", 0)])
        self.colored()
        res = subprocess.run(
            [*PY, "-c", ATTACH, json.dumps(TABLE), "w1", "more"],
            env=self.server.child_env(),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=WAIT,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(
            json.loads(res.stdout), [SID, True, "One.", repr(TurnEnd("Two.", 0)), "Two."]
        )
        self.assertEqual([stream.next() for _ in range(2)], [TurnStarted(), TurnEnd("Two.", 0)])
        self.assertEqual(handle.reply, "Two.")
        self.lines("w1 done", "w1 done")
        log = RunLog(self.workdir).read()
        self.assertEqual([e["client_id"] for e in log if e["kind"] == "client_session"], [SID])


class ShowTest(Live):
    def test_show_from_inside_the_manager_puts_it_in_the_grid_close_takes_it_out(self):
        self.server.attach(MANAGER)
        handle = self.start("w1", scenario={"steps": ["Hi."]})
        self.assertEqual(self.panes(), 1)
        with mock.patch.dict(os.environ, self.server.inside(MANAGER)):
            self.assertIsNone(self.show("w1"))
        live_tmux.wait(
            functools.partial(live_tmux.assert_grid, self, self.server, MANAGER, [["w1"]]),
            what="the grid [['w1']]",
        )
        self.assertEqual(self.server.opener_of("w1"), MANAGER)
        self.session_cls.retire("w1")

        def alone() -> bool:
            self.poke()
            return self.panes() == 1

        live_tmux.wait(alone, what="the manager's window back to its own pane")
        self.closed(handle)

    def test_with_no_terminal_show_gives_the_attach_command_and_close_makes_it_absent(self):
        handle = self.start("w1", scenario={"steps": ["Hi."]})
        stream = self.stream(handle)
        self.assertEqual(
            [stream.next() for _ in range(2)],
            [TurnStarted(), TurnEnd("Hi.", 0)],
        )
        with self.assertNoLogs("session", "WARNING"):
            self.assertEqual(self.show("w1"), guard_command("w1"))
        with (
            mock.patch.dict(os.environ, self.server.inside(MANAGER)),
            self.assertLogs("session", "WARNING") as logs,
        ):
            self.assertEqual(self.show("w1"), guard_command("w1"))
        self.assertIn(f"no terminal shows tmux session {MANAGER}", logs.output[0])
        self.assertEqual((self.panes(), self.server.pane_of("w1")), (1, ""))
        self.session_cls.retire("w1")
        self.assertEqual(stream.until_exited(), [Exited(-signal.SIGHUP)])
        self.closed(handle)
        with self.assertRaises(SessionError):
            self.show("w1")


if __name__ == "__main__":
    unittest.main()
