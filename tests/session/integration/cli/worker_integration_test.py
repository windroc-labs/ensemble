"""ensemble-session's worker lifecycle through bin/ensemble-session on a private tmux server: the
client's exact cwd, argv and env from start (Q03), reply from another process (Q09), a client
that exits at once (Q10, D3), a start whose early watch ends on a dialog (a Claude hook, the
Codex trust screen), and a Claude start's /color: quiet once sent, said when a dialog holds it.
"""

import json
import os
import shutil
import tempfile

from ensemble.session.harness.channel import Channel
from tests.session import live_tmux
from tests.session.case import REPO
from tests.session.cli_support import MANAGER, WorkerCase, logged
from tests.session.integration.clients.codex import fake_tui

PERMISSION = {"kind": "hook", "event": "PermissionRequest", "tool_name": "Edit"}
TRUST = "Quick safety check: Is this a project you created or one you trust? 1. Yes, I trust this folder 2. No, exit"
ATTACH = "session w1: tmux attach -t '=w1'"


class StartArgvTest(WorkerCase):
    def test_start_runs_the_client_with_the_given_cwd_prompt_flags_and_env(self):
        dump = self.dumping()
        sub = os.path.join(self.cwd, "sub")
        os.mkdir(sub)
        prompt = "line one\n  'single' \"double\" $HOME `date` \\ end"
        res = self.session(
            "start",
            "w1",
            "--client",
            "claude",
            "--cwd",
            "sub",
            "--prompt",
            prompt,
            "--env",
            "WORKER_EMPTY=",
            "--env",
            "WORKER_TWICE=1",
            "--env",
            "WORKER_TWICE=2",
            "--",
            "--model",
            "-x y",
            "--effort=two words",
            ENSEMBLE_EVENTS=self.events,
            **self.scene("w1"),
        )
        record = self.record(res)
        (call,) = self.calls("w1")
        self.assertEqual(call["cwd"], sub)
        self.claude_argv(
            call["argv"], record.sid, "w1", "--model", "-x y", "--effort=two words", "--", prompt
        )
        (dumped,) = logged(dump)
        self.assertEqual(dumped["argv"], call["argv"])
        self.assertEqual(
            {key: dumped["env"].get(key) for key in ("WORKER_EMPTY", "WORKER_TWICE")},
            {"WORKER_EMPTY": "", "WORKER_TWICE": "2"},
        )

    def test_start_without_cwd_or_prompt_runs_in_the_callers_directory_with_no_prompt(self):
        here = os.path.join(self.cwd, "caller")
        os.mkdir(here)
        res = self.session(
            "start", "w1", "--client", "claude", cwd=here, **self.scene("w1", turns=[])
        )
        record = self.record(res)
        (call,) = self.calls("w1")
        self.assertEqual(call["cwd"], here)
        self.claude_argv(call["argv"], record.sid, "w1")


class ReplyTest(WorkerCase):
    def test_reply_prints_the_latest_turns_reply_from_another_process(self):
        steps = ["Not the reply.", {"kind": "tool", "name": "Bash"}, "Hi from w1."]
        self.record(self.start("w1", steps=steps, turns=[["Second."]]))
        self.wait_event("w1", "done")
        res = self.ok(self.session("reply", "w1"))
        self.assertEqual((res.stdout, res.stderr), ("Hi from w1.\n", ""))
        self.ok(self.session("send", "w1", "more"))
        self.wait_event("w1", "done", 2)
        res = self.ok(self.session("reply", "w1", inside=None))
        self.assertEqual((res.stdout, res.stderr), ("Second.\n", ""))
        self.assertEqual(len(self.calls("w1")), 1)

    def test_reply_of_a_worker_with_no_reply_yet_prints_nothing(self):
        steps = [PERMISSION, {"kind": "dialog", "text": "Allow this edit?"}]
        self.record(self.start("w1", steps=steps))
        self.wait_event("w1", "blocked")
        res = self.session("reply", "w1")
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""))

    def test_reply_of_an_absent_session_exits_1(self):
        res = self.session("reply", "w9")
        self.assertEqual((res.returncode, res.stdout), (1, ""), res.stderr)
        self.assertRegex(res.stderr, r"\Aensemble-session: no tui session w9\b[^\n]*\n\Z")


class EarlyDeathTest(WorkerCase):
    def test_a_client_that_exits_at_once_exits_1_with_its_last_lines_and_keeps_its_pane(self):
        res = self.start("w2", "--", "--bogus")
        self.assertEqual((res.returncode, res.stdout), (1, ""), res.stderr)
        lines = res.stderr.splitlines()
        head = "ensemble-session: w2: claude exited 2 at once; its last lines:"
        self.assertIn(head, lines, res.stderr)
        self.assertEqual(lines[: lines.index(head)], ["session w2: tmux attach -t '=w2'"])
        self.assertIn(
            "claude: error: unrecognized arguments: --bogus", lines[lines.index(head) + 1 :]
        )
        self.assertTrue(self.live("w2"))
        self.assertEqual(self.pane("w2", "#{pane_dead}"), "1")
        self.assertEqual(self.ok(self.session("ls", "--records")).stdout, "")
        self.assertEqual(self.server.option("w2", "@record"), "")


class DialogTest(WorkerCase):
    def test_a_permission_hook_ends_the_early_watch_and_start_prints_the_record(self):
        steps = [PERMISSION, {"kind": "dialog", "text": "Allow this edit?"}, "Edited."]
        res = self.start("w1", steps=steps)
        record = self.record(res)
        self.assertEqual((record.name, record.detail), ("w1", "claude"))
        self.assertIn("session w1: waits on a dialog: Edit", res.stderr.splitlines())
        self.wait_event("w1", "blocked")
        self.assertEqual(self.words(), [["w1", "blocked"]])
        self.assertEqual(self.server.option("w1", "@state"), "blocked")
        self.ok(self.session("send", "w1", "yes"))
        self.wait_event("w1", "done")
        self.assertEqual(self.words(), [["w1", "blocked"], ["w1", "done"]])
        self.assertEqual(self.ok(self.session("reply", "w1")).stdout, "Edited.\n")

    def test_the_codex_trust_dialog_ends_the_early_watch_and_start_prints_the_record(self):
        # Codex may write /tmp and $TMPDIR, where the fixture's HOME lies; Codex's recovery state
        # must lie elsewhere
        root = tempfile.mkdtemp(dir=os.environ.get("ENSEMBLE_TEST_TMP", REPO))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self.addCleanup(self.server.kill)
        home, bin_dir = os.path.join(root, "home"), os.path.join(root, "bin")
        for path in (home, bin_dir):
            os.mkdir(path)
        shutil.copy2(self.server.program, bin_dir)
        fake_tui.install(bin_dir)
        never, scenario, events = (
            os.path.join(root, name) for name in ("never", "w1.codex.json", "events")
        )
        trust = "Trust this folder? Codex can read, edit, and run files here.\n\n› 1. Trust and continue"
        steps = [{"kind": "screen", "text": trust}, {"kind": "pause", "file": never}]
        with open(scenario, "w", encoding="utf-8") as f:
            json.dump({"tui_turns": [steps]}, f)
        res = self.session(
            "start",
            "w1",
            "--client",
            "codex",
            "--cwd",
            self.cwd,
            "--prompt",
            "hi",
            HOME=home,
            PATH=bin_dir + os.pathsep + self.env()["PATH"],
            ENSEMBLE_EVENTS=events,
            FAKE_SCENARIO=scenario,
            FAKE_LOG=os.path.join(root, "w1.codex.jsonl"),
        )
        record = self.record(res)
        self.assertEqual((record.name, record.detail, record.display), ("w1", "codex", MANAGER))
        self.assertIn("session w1: waits on a dialog: Trust this folder?", res.stderr.splitlines())
        self.wait_event("w1", "blocked", events=events)
        self.assertEqual(self.server.option("w1", "@state"), "blocked")


class ColorTest(WorkerCase):
    def test_start_colors_quietly(self):
        res = self.start("w1")
        self.assertEqual((self.record(res).name, res.stderr), ("w1", ATTACH + "\n"))
        (call,) = self.calls("w1")
        colored = live_tmux.wait(lambda: self.commands("w1"), what="w1's /color")
        self.assertEqual(
            [(c["pid"], c["line"], c["dialog"]) for c in colored], [(call["pid"], "/color", False)]
        )

    def test_exit_after_color_still_fails_start(self):
        res = self.start("w1", steps=[{"kind": "sleep", "seconds": 3}, {"kind": "exit"}])
        self.assertEqual((res.returncode, res.stdout), (1, ""), res.stderr)
        lines = res.stderr.splitlines()
        head = "ensemble-session: w1: claude exited 0 at once; its last lines:"
        self.assertIn(head, lines, res.stderr)
        self.assertEqual(lines[: lines.index(head)], [ATTACH], res.stderr)
        self.assertIn("/color", lines[lines.index(head) + 1 :])
        self.assertEqual(self.ok(self.session("ls", "--records")).stdout, "")

    def test_dialog_start_says_color_not_set(self):
        res = self.start("w1", steps=[{"kind": "dialog", "text": TRUST}, "Trusted."])
        self.assertEqual(self.record(res).name, "w1")
        self.assertEqual(
            res.stderr.splitlines(),
            [
                ATTACH,
                "session w1: waits on a dialog: Quick safety check",
                "session w1: color not set: waits on a dialog; ensemble-session send w1 '/color'",
            ],
        )
        self.assertEqual(self.commands("w1"), [])


class HarnessedWorkerTest(WorkerCase):
    def test_a_claude_worker_runs_under_a_harness_and_closes_through_it(self):
        record = self.record(self.start("w1"))
        with open(os.path.join(self.ensemble, "tui", "w1", "session.json"), encoding="utf-8") as f:
            channel = Channel(json.load(f)["owner"])
        self.assertEqual(channel.status()["sid"], record.sid)
        self.assertTrue(channel.alive())
        self.ok(self.session("close", "w1"))
        self.assertFalse(self.live("w1"))
        self.assertEqual(logged(channel.events)[-1]["type"], "Exited")
        live_tmux.wait(lambda: not channel.alive(), timeout=40, what="the harness to exit")
