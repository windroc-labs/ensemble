"""Session behavior tests with caller-owned dependencies."""

import contextlib
import dataclasses
import glob
import itertools
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from unittest import mock

from ensemble.session.clients.claude import ClaudeClient
from ensemble.session.clients.claude.tui import ClaudeTui
from ensemble.session.engine.types import Access, Spec
from tests.session import case, live_tmux
from tests.session.integration.clients.claude import fake_claude

FAKE = os.path.abspath(fake_claude.__file__)

SID = "11111111-2222-4333-8444-555555555555"

SESSION = ("--session-id", SID)

ARGS = ["-p", "hi", *SESSION, "--output-format", "stream-json", "--verbose"]

RESUME = ["-p", "hi", "--resume", SID, "--output-format", "stream-json", "--verbose"]

TRUST = "Quick safety check: Is this a project you created or one you trust?"

LIMIT = "You've hit your session limit. Your limit resets 3pm (Europe/Berlin)"


def said(content) -> str:
    if isinstance(content, str):
        return content
    (block,) = content
    return block["text"] if block["type"] == "text" else f"{block['type']} {block['name']}"


def gone(pid: int) -> bool:
    res = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=30
    )
    return res.stdout.strip() == "" or res.stdout.strip().startswith("Z")


class Case(case.Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.proj, self.config = os.path.join(self.root, "proj"), os.path.join(self.root, "config")
        os.mkdir(self.proj)
        self.scenario, self.hooklog, self.log = (
            os.path.join(self.root, f) for f in ("scenario.json", "hooks.jsonl", "log.jsonl")
        )
        self.env = {
            "PATH": "/usr/bin:/bin",
            "HOME": self.root,
            "CLAUDE_CONFIG_DIR": self.config,
            fake_claude.ENV: self.scenario,
        }

    def scene(self, **scenario) -> None:
        with open(self.scenario, "w", encoding="utf-8") as f:
            json.dump({"log": self.log, **scenario}, f)

    def tag(self, name: str) -> dict:
        return {"type": "command", "command": f"echo {name} >> {shlex.quote(self.hooklog)}"}

    def settings(self, *events: str) -> str:
        q = shlex.quote(self.hooklog)
        hook = {"type": "command", "command": f"cat >> {q}; echo >> {q}"}
        return json.dumps({"hooks": {e: [{"hooks": [hook]}] for e in events}})

    def run_fake(self, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
        return self.run_argv((sys.executable, "-I", FAKE, *args), stdin)

    def run_argv(self, argv: tuple[str, ...], stdin: str = "") -> subprocess.CompletedProcess:
        return subprocess.run(
            argv,
            cwd=self.proj,
            env=self.env,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def popen(self, *args: str) -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, "-I", FAKE, *args],
            cwd=self.proj,
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self.reap, proc)
        return proc

    def reap(self, proc: subprocess.Popen) -> None:
        proc.kill()
        proc.wait()
        for pipe in filter(None, (proc.stdin, proc.stdout, proc.stderr)):
            with contextlib.suppress(OSError):
                pipe.close()

    def logged(self) -> list[dict]:
        return [json.loads(line) for line in live_tmux.events(self.log)]

    def transcripts(self) -> list[str]:
        return glob.glob(os.path.join(self.config, "projects", "*", f"{SID}.jsonl"))

    def convo(self) -> list[tuple[str, str]]:
        (path,) = self.transcripts()
        with open(path, encoding="utf-8") as f:
            return [(r["type"], said(r["message"]["content"])) for r in map(json.loads, f)]

    def users(self) -> list[str]:
        return [text for role, text in self.convo() if role == "user"]

    def fired(self) -> list[dict]:
        return [json.loads(line) for line in live_tmux.events(self.hooklog) if line]

    def names(self) -> list[str]:
        return [h["hook_event_name"] for h in self.fired()]

    def result(self, res: subprocess.CompletedProcess) -> str:
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout.splitlines()[-1])["result"]


class Hooks(Case):
    def test_payloads_and_order(self):
        self.scene(
            steps=[
                {"kind": "tool", "name": "Read"},
                {
                    "kind": "hook",
                    "event": "PermissionRequest",
                    "tool_name": "Bash",
                    "cwd": "/x",
                    "n": [1],
                },
                "Done.",
            ],
            turns=[["Next."], []],
            background=2,
        )
        events = ("UserPromptSubmit", "PostToolUse", "PermissionRequest", "Stop")
        res = self.run_fake(
            *SESSION, "--settings", self.settings(*events), "--", "hi", stdin="next\nlast\n"
        )
        self.assertEqual((res.returncode, res.stdout), (0, "Done.\nNext.\n"), res.stderr)
        (path,) = self.transcripts()

        def at(event: str, **fields) -> dict:
            return {
                "session_id": SID,
                "transcript_path": path,
                "cwd": self.proj,
                "hook_event_name": event,
                **fields,
            }

        def stop(last: str) -> dict:
            return at(
                "Stop",
                stop_hook_active=False,
                last_assistant_message=last,
                background_tasks=[{}, {}],
            )

        fired = self.fired()
        self.assertEqual(
            fired,
            [
                at("UserPromptSubmit", prompt="hi"),
                at("PostToolUse", tool_name="Read"),
                at("PermissionRequest", tool_name="Bash", cwd="/x", n=[1]),
                stop("Done."),
                at("UserPromptSubmit", prompt="next"),
                stop("Next."),
                at("UserPromptSubmit", prompt="last"),
                stop(""),
            ],
        )
        self.assertEqual(
            (type(fired[3]["stop_hook_active"]), fired[3]["stop_hook_active"]), (bool, False)
        )
        self.assertEqual(
            self.convo(),
            [
                ("user", "hi"),
                ("assistant", "tool_use Read"),
                ("assistant", "Done."),
                ("user", "next"),
                ("assistant", "Next."),
                ("user", "last"),
            ],
        )

    def test_matchers_and_malformed_entries(self):
        tag = self.tag
        entries = [
            {"matcher": "^x$", "hooks": [tag("skipped")]},
            {"matcher": None, "hooks": [tag("null")]},
            "text",
            {"matcher": "*"},
            {"hooks": "no list"},
            {
                "hooks": [
                    "text",
                    {"type": "command"},
                    {**tag("http"), "type": "http"},
                    {**tag("five"), "command": 5},
                ]
            },
            {"matcher": "*", "hooks": [tag("star")]},
            {"matcher": "", "hooks": [tag("empty")]},
            {"hooks": [tag("absent")]},
            {"hooks": [tag("a"), tag("b")]},
        ]
        self.scene()
        settings = json.dumps({"hooks": {"Stop": entries, "UserPromptSubmit": "not a list"}})
        res = self.run_fake("hi", *SESSION, "--settings", settings)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(live_tmux.events(self.hooklog), ["star", "empty", "absent", "a", "b"])

    def test_a_regex_matcher_searches_the_tool_name_or_notification_type(self):
        def entry(matcher: str, name: str) -> dict:
            return {"matcher": matcher, "hooks": [self.tag(name)]}

        hooks = {
            "PostToolUse": [entry("^Rea", "rea"), entry("Edit|Write", "edit"), entry("(", "bad")],
            "Notification": [entry(ClaudeTui.MATCHER, "note")],
            "PermissionRequest": [entry("Bash", "ask")],
        }
        self.scene(
            steps=[
                *({"kind": "tool", "name": n} for n in ("Read", "Write", "Grep", "Bread")),
                *(
                    {"kind": "hook", "event": "Notification", "notification_type": t}
                    for t in ("idle_prompt", "permission_prompt", "idle_prompt_x")
                ),
                {"kind": "hook", "event": "Notification"},
                *(
                    {"kind": "hook", "event": "PermissionRequest", "tool_name": n}
                    for n in ("Read", "Bash")
                ),
            ]
        )
        res = self.run_fake("hi", *SESSION, "--settings", json.dumps({"hooks": hooks}))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(live_tmux.events(self.hooklog), ["rea", "edit", "note", "ask"])

    def test_failing_hooks_do_not_stop_the_fake(self):
        marker = shlex.quote(self.hooklog)
        commands = [
            "exit 1",
            "cat >/dev/null; kill -9 $$",
            "no-such-command-anywhere",
            f"echo after >> {marker}",
        ]
        settings = json.dumps(
            {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": c} for c in commands]}]}}
        )
        self.scene(steps=["Hi."])
        res = self.run_fake("hi", *SESSION, "--settings", settings, stdin="again\n")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(live_tmux.events(self.hooklog), ["after", "after"])
        self.assertEqual(self.users(), ["hi", "again"])

    def test_cwd_env_stdout_and_stderr(self):
        command = (
            'echo "$PWD $MARK" >> '
            + shlex.quote(self.hooklog)
            + "; echo to-stdout; echo to-stderr >&2"
        )
        self.env["MARK"] = "m"
        self.scene()
        res = self.run_fake(
            "hi",
            *SESSION,
            "--settings",
            json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": command}]}]}}),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(live_tmux.events(self.hooklog), [f"{self.proj} m"])
        self.assertNotIn("to-stdout", res.stdout)
        self.assertIn("to-stderr", res.stderr)

    def test_settings_without_usable_hooks(self):
        self.scene(steps=[{"kind": "hook", "event": "Stop"}])
        for settings in (("--settings", '{"hooks": "x"}'), ("--settings", "{}"), ()):
            with self.subTest(settings=settings):
                res = self.run_fake("hi", *SESSION, *settings)
                self.assertEqual(res.returncode, 0, res.stderr)


class Steps(Case):
    def test_a_dialog_prints_and_waits_for_its_answer(self):
        self.scene(steps=[{"kind": "dialog", "text": TRUST}, "Hi."], turns=[["Again."]])
        proc = self.popen(
            *SESSION, "--settings", self.settings("UserPromptSubmit", "Stop"), "--", "hello"
        )
        self.assertEqual(proc.stdout.readline(), TRUST + "\n")
        time.sleep(0.5)
        self.assertEqual(self.names(), ["UserPromptSubmit"])
        out, err = proc.communicate("yes\nmore\n", timeout=30)
        self.assertEqual((proc.returncode, out), (0, "Hi.\nAgain.\n"), err)
        self.assertEqual(self.names(), ["UserPromptSubmit", "Stop", "UserPromptSubmit", "Stop"])
        self.assertEqual(
            self.convo(),
            [("user", "hello"), ("assistant", "Hi."), ("user", "more"), ("assistant", "Again.")],
        )

    def test_an_exit_step_or_eof_at_a_dialog_exits_at_once_with_exit(self):
        for name, steps, stdout in (
            ("exit step", ["Hi.", {"kind": "exit"}, "Never."], "Hi.\n"),
            ("eof at a dialog", [{"kind": "dialog", "text": TRUST}, "Hi."], TRUST + "\n"),
        ):
            with self.subTest(name):
                self.scene(steps=steps, exit=4)
                res = self.run_fake(*SESSION, "--settings", self.settings("Stop"), "--", "hello")
                self.assertEqual(
                    (res.returncode, res.stdout, self.fired()), (4, stdout, []), res.stderr
                )

    def test_a_limit_ends_the_turn_with_stop_failure_and_an_api_error(self):
        self.scene(
            steps=["Working.", {"kind": "limit", "text": LIMIT}, "Skipped."], turns=[["After."]]
        )
        hooks = self.settings("UserPromptSubmit", "Stop", "StopFailure")
        proc = self.popen(*SESSION, "--settings", hooks, "--", "hello")
        self.assertEqual([proc.stdout.readline() for _ in range(2)], ["Working.\n", LIMIT + "\n"])
        time.sleep(0.5)
        self.assertIsNone(proc.poll())
        self.assertEqual(self.names(), ["UserPromptSubmit", "StopFailure"])
        failure = self.fired()[-1]
        self.assertEqual(
            (failure["error"], failure["last_assistant_message"]), ("rate_limit", LIMIT)
        )
        out, err = proc.communicate("again\n", timeout=30)
        self.assertEqual((proc.returncode, out), (0, "After.\n"), err)
        self.assertEqual(
            self.names(), ["UserPromptSubmit", "StopFailure", "UserPromptSubmit", "Stop"]
        )
        self.assertEqual(
            self.convo(),
            [
                ("user", "hello"),
                ("assistant", "Working."),
                ("assistant", LIMIT),
                ("user", "again"),
                ("assistant", "After."),
            ],
        )
        (path,) = self.transcripts()
        with open(path, encoding="utf-8") as f:
            error = [json.loads(line) for line in f][2]
        self.assertEqual((error["isApiErrorMessage"], error["error"]), (True, "rate_limit"))

    def test_sleep(self):
        self.scene(steps=[{"kind": "sleep", "seconds": 0.5}, "Hi."])
        start = time.monotonic()
        res = self.run_fake(*SESSION, "--", "hello")
        self.assertGreaterEqual(time.monotonic() - start, 0.5)
        self.assertEqual((res.returncode, res.stdout), (0, "Hi.\n"), res.stderr)

    def joined(self) -> list[tuple[str, str]]:
        (path,) = self.transcripts()
        with open(path, encoding="utf-8") as f:
            records = [json.loads(line) for line in f]
        return [
            ("enqueue", r["content"])
            if r["type"] == "queue-operation"
            else (r["attachment"]["commandMode"], r["attachment"]["prompt"])
            for r in records
            if r["type"] in ("queue-operation", "attachment")
        ]

    def prompts(self) -> list[tuple[str, str | None]]:
        return [(h["hook_event_name"], h.get("prompt")) for h in self.fired()]

    def queued(self, **step) -> list[tuple[str, str | None]]:
        """A queued step's hooks, its line `more`, once it ran and the fake ended."""
        self.scene(steps=[{"kind": "queued", "answer": "Noted.", **step}, "Done."])
        hooks = self.settings("UserPromptSubmit", "PostToolUse", "Stop")
        res = self.run_fake(*SESSION, "--settings", hooks, "--", "hi", stdin="more\n")
        self.assertEqual((res.returncode, res.stdout), (0, "Noted.\nDone.\n"), res.stderr)
        self.assertEqual(self.joined(), [("enqueue", "more"), ("prompt", "more")])
        return self.prompts()

    def test_a_queued_line_joins_the_turn_with_no_hook(self):
        self.assertEqual(
            self.queued(prompted=False),
            [("UserPromptSubmit", "hi"), ("PostToolUse", None), ("Stop", None)],
        )

    def test_a_prompted_queued_line_runs_user_prompt_submit_as_the_turn_joins_it(self):
        self.assertEqual(
            self.queued(prompted=True),
            [
                ("UserPromptSubmit", "hi"),
                ("PostToolUse", None),
                ("UserPromptSubmit", "more"),
                ("Stop", None),
            ],
        )

    def test_a_notice_joins_the_turn_with_its_hook_reading_no_input(self):
        notice = "<task-notification>sleep 30 completed</task-notification>"
        self.scene(steps=[{"kind": "notice", "text": notice}, "Done."], turns=[["Next."]])
        hooks = self.settings("UserPromptSubmit", "Stop")
        res = self.run_fake(*SESSION, "--settings", hooks, "--", "hi", stdin="again\n")
        self.assertEqual((res.returncode, res.stdout), (0, "Done.\nNext.\n"), res.stderr)
        self.assertEqual(
            self.prompts(),
            [
                ("UserPromptSubmit", "hi"),
                ("UserPromptSubmit", notice),
                ("Stop", None),
                ("UserPromptSubmit", "again"),
                ("Stop", None),
            ],
        )
        self.assertEqual(self.joined(), [("task-notification", notice)])


class Pane(Case):
    def test_lines_typed_into_a_tmux_pane_are_turns(self):
        server = live_tmux.Server(self, self.home)
        server.start("mgr")
        self.scene(steps=["Hi."], turns=[["Again."]])
        hook = {
            "type": "command",
            "command": f'printf "%s %s\\n" "$TMUX_PANE" "$(cat)" >> {shlex.quote(self.hooklog)}',
        }
        res = server.tmux(
            "new-session",
            "-d",
            "-s",
            "fake",
            "-c",
            self.proj,
            "-e",
            f"{fake_claude.ENV}={self.scenario}",
            "-e",
            f"CLAUDE_CONFIG_DIR={self.config}",
            "claude",
            *SESSION,
            "--settings",
            json.dumps({"hooks": {"Stop": [{"hooks": [hook]}]}}),
            "--",
            "hello",
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        pane = server.inside("fake")["TMUX_PANE"]

        def stops(n: int) -> list[tuple[str, dict]]:
            rows = [line.split(" ", 1) for line in live_tmux.events(self.hooklog) if line]
            return len(rows) == n and [(p, json.loads(payload)) for p, payload in rows]

        live_tmux.wait(lambda: stops(1), what="turn 1's Stop")
        for keys in (("-l", "next"), ("Enter",)):
            self.assertEqual(server.tmux("send-keys", "-t", "=fake:", *keys).returncode, 0)
        (first, one), (second, two) = live_tmux.wait(lambda: stops(2), what="turn 2's Stop")
        self.assertEqual((first, second), (pane, pane))
        self.assertEqual(
            (one["last_assistant_message"], two["last_assistant_message"]), ("Hi.", "Again.")
        )
        screen = server.tmux("capture-pane", "-p", "-t", "=fake:").stdout
        self.assertLess(screen.index("Hi."), screen.index("next"))
        self.assertLess(screen.index("next"), screen.index("Again."))
        self.assertEqual(self.users(), ["hello", "next"])


class ClaudeTuiLaunch(Case):
    def test_its_argv_runs_and_its_hooks_fit_its_readers(self):
        bin_dir = os.path.join(self.root, "bin")
        os.mkdir(bin_dir)
        self.env["PATH"] = fake_claude.install(bin_dir)
        with open(os.path.join(self.proj, ".mcp.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        patch = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.config})
        patch.start()
        self.addCleanup(patch.stop)
        tui = ClaudeClient(
            {
                "allow": ["Read"],
                "tiers": {"1": "m"},
                "efforts": {"high": "h"},
            },
        ).tui()
        spec = Spec(
            workdir=self.proj,
            flags=("--permission-mode", "auto", "--strict-mcp-config"),
            sid=SID,
            tier=1,
            effort="high",
            access=Access(cwd=self.proj, dirs=(self.root,), commands=("git status",), project=True),
        )
        q = shlex.quote(self.hooklog)
        hook = ("/bin/sh", "-c", f'printf "%s " "$0" >> {q}; cat >> {q}; echo >> {q}')
        self.scene(
            steps=[
                {"kind": "tool", "name": "Read"},
                {"kind": "hook", "event": "PermissionRequest", "tool_name": "Bash"},
                *(
                    {
                        "kind": "hook",
                        "event": "Notification",
                        "notification_type": t,
                        "message": "Needs you",
                    }
                    for t in ("idle_prompt", "permission_prompt")
                ),
                "Done.",
            ],
            background=1,
        )
        new, resume = (
            tui.launch(dataclasses.replace(spec, prompt="do it"), hook),
            tui.launch(dataclasses.replace(spec, resume=True), hook),
        )
        self.assertEqual(new.argv[-2:], ("--", "do it"))
        res = self.run_argv(new.argv)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.scene()
        res = self.run_argv(resume.argv, "more\n")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.users(), ["do it", "more"])
        labels = [line.split(" ", 1) for line in live_tmux.events(self.hooklog) if line]
        self.assertEqual(
            [label for label, _ in labels],
            [
                "SessionStart",
                "UserPromptSubmit",
                "PostToolUse",
                "PermissionRequest",
                "Notification",
                "Stop",
                "SessionStart",
                "UserPromptSubmit",
                "Stop",
            ],
        )
        self.assertEqual(
            [json.loads(p)["source"] for label, p in labels if label == "SessionStart"],
            ["startup", "resume"],
        )
        payloads = {label: json.loads(payload) for label, payload in labels[:6]}
        self.assertEqual(payloads["Notification"]["notification_type"], "permission_prompt")
        self.assertEqual(
            (
                tui.activity(payloads["PostToolUse"]),
                tui.blocked_reason(payloads["PermissionRequest"]),
                tui.blocked_reason(payloads["Notification"]),
            ),
            ("Read", "Bash", "Needs you"),
        )
        stop = payloads["Stop"]
        self.assertEqual(
            (tui.reply(stop), tui.pending(stop), stop["transcript_path"]),
            ("Done.", 1, new.transcript),
        )


class BadScenario(Case):
    def test_a_bad_step_or_value_exits_1_before_the_session(self):
        tui = [
            {"kind": "hook", "event": "Stop"},
            {"kind": "dialog", "text": "x"},
            {"kind": "limit", "text": "x"},
            {"kind": "sleep", "seconds": 0},
            {"kind": "queued", "answer": "x", "prompted": True},
            {"kind": "notice", "text": "x"},
        ]
        rows = [
            *({"steps": [step]} for step in tui),
            {"steps": "x"},
            {"steps": [1]},
            {"steps": [{"kind": "nope"}]},
            {"steps": [{"kind": "tool"}]},
            {"steps": [{"kind": "tool", "name": 7}]},
            {"steps": [{"kind": "tool", "name": "R", "x": 1}]},
            {"steps": [{"kind": "limit"}]},
            {"steps": [{"kind": "limit", "resets_at": "soon"}]},
            {"steps": [{"kind": "limit", "resets_at": True}]},
            {"steps": [{"kind": "write", "path": "a"}]},
            {"steps": [{"kind": "write", "path": 1, "text": "x"}]},
            {"steps": [{"kind": "write", "path": "", "text": "x"}]},
            {"steps": [{"kind": "write", "path": "a", "text": None}]},
            {"steps": [{"kind": "run", "argv": ["true"]}]},
            {"steps": [{"kind": "run", "argv": [], "path": "a"}]},
            {"steps": [{"kind": "run", "argv": "true", "path": "a"}]},
            {"steps": [{"kind": "run", "argv": [1], "path": "a"}]},
            {"steps": [{"kind": "run", "argv": ["true"], "path": ""}]},
            {"resume": "x"},
            {"resume": [1]},
            {"turns": "x"},
            {"turns": ["x"]},
            {"background": -1},
            {"background": True},
            {"rate_limit": []},
            {"exit": "0"},
            {"exit": True},
            {"hang": 1},
            {"log": 5},
            {"gate": 5},
        ]
        interactive = (
            {"turns": [[1]]},
            {"turns": [["ok"], [{"kind": "nope"}]]},
            {"turns": [[{"kind": "hook"}]]},
            {"steps": [{"kind": "hook", "event": ""}]},
            {"steps": [{"kind": "hook", "event": 7}]},
            {"steps": [{"kind": ["hook"], "event": "Stop"}]},
            {"steps": [{"kind": "limit", "resets_at": None}]},
            {"steps": [{"kind": "limit", "text": 5}]},
            {"steps": [{"kind": "dialog"}]},
            {"steps": [{"kind": "dialog", "text": "x", "y": 1}]},
            {"steps": [{"kind": "sleep", "seconds": -1}]},
            {"steps": [{"kind": "sleep", "seconds": "1"}]},
            {"steps": [{"kind": "sleep", "seconds": True}]},
            {"steps": [{"kind": "sleep"}]},
            {"steps": [{"kind": "queued", "answer": "x", "prompted": 1}]},
            {"steps": [{"kind": "queued", "prompted": True}]},
            {"steps": [{"kind": "notice"}]},
            {"steps": [{"kind": "notice", "text": 5}]},
            {"steps": [{"kind": "notice", "text": "x", "prompted": True}]},
            {"steps": [{"kind": "write", "path": "a", "text": "x"}]},
            {"steps": [{"kind": "run", "argv": ["true"], "path": "a"}]},
        )
        for args, scenario in (
            *((ARGS, scenario) for scenario in rows),
            *(((*SESSION, "--", "hi"), scenario) for scenario in interactive),
        ):
            with self.subTest(args=args, scenario=scenario):
                self.scene(**scenario)
                res = self.run_fake(*args)
                self.assertEqual((res.returncode, res.stdout), (1, ""), res.stderr)
                self.assertIn("fake_claude.py: bad scenario", res.stderr)
                self.assertFalse(os.path.exists(self.config))


class PrintMode(Case):
    def test_valid_steps(self):
        self.scene(
            steps=[
                "Hi.",
                {"kind": "tool", "name": "Read"},
                {"kind": "limit", "resets_at": None},
                {"kind": "limit", "resets_at": 1.5},
            ],
            turns=[[{"kind": "dialog", "text": "x"}]],
        )
        res = self.run_fake(*ARGS, "--settings", self.settings("UserPromptSubmit", "Stop"))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(
            [json.loads(line)["type"] for line in res.stdout.splitlines()],
            [
                "system",
                "assistant",
                "assistant",
                "rate_limit_event",
                "assistant",
                "rate_limit_event",
                "assistant",
                "result",
            ],
        )
        self.assertEqual(self.fired(), [])

    def test_a_write_step_writes_its_file_under_the_cwd_then_the_next_step_runs(self):
        self.scene(steps=[{"kind": "write", "path": "sub/out.md", "text": "# Out\n"}, "After."])
        res = self.run_fake(*ARGS)
        self.assertEqual(self.result(res), "After.")
        self.assertEqual(
            [json.loads(line)["type"] for line in res.stdout.splitlines()],
            ["system", "assistant", "result"],
        )
        with open(os.path.join(self.proj, "sub", "out.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "# Out\n")

    def test_a_run_step_writes_its_commands_result_under_the_cwd_then_the_next_step_runs(self):
        argv = ["/bin/sh", "-c", "pwd -P; printf oops >&2; exit 3"]
        self.scene(steps=[{"kind": "run", "argv": argv, "path": "ran.json"}, "After."])
        res = self.run_fake(*ARGS)
        self.assertEqual(self.result(res), "After.")
        with open(os.path.join(self.proj, "ran.json"), encoding="utf-8") as f:
            self.assertEqual(
                json.load(f),
                {"returncode": 3, "stdout": os.path.realpath(self.proj) + "\n", "stderr": "oops"},
            )

    def test_a_run_step_that_cannot_start_records_why_in_a_new_directory(self):
        missing = os.path.join(self.root, "no-such-command")
        self.scene(steps=[{"kind": "run", "argv": [missing], "path": "sub/ran.json"}, "After."])
        self.assertEqual(self.result(self.run_fake(*ARGS)), "After.")
        with open(os.path.join(self.proj, "sub", "ran.json"), encoding="utf-8") as f:
            ran = json.load(f)
        self.assertEqual((ran["returncode"], ran["stdout"]), (None, ""))
        self.assertIn(missing, ran["stderr"])

    def test_resume_steps_replace_steps_only_on_a_resume(self):
        self.scene(steps=["First."], resume=["Again."])
        self.assertEqual(self.result(self.run_fake(*ARGS)), "First.")
        self.assertEqual(self.result(self.run_fake(*RESUME)), "Again.")
        self.scene(steps=["First."])
        self.assertEqual(self.result(self.run_fake(*RESUME)), "First.")


class Interactive(Case):
    def test_turns_from_stdin(self):
        self.scene(steps=["First."], turns=[["Second."]], exit=3)
        res = self.run_fake(*SESSION, "--", "hello", stdin="line1\nline2\n")
        self.assertEqual(res.returncode, 3, res.stderr)
        self.assertEqual(res.stdout, "First.\nSecond.\n")
        self.assertEqual(
            self.convo(),
            [
                ("user", "hello"),
                ("assistant", "First."),
                ("user", "line1"),
                ("assistant", "Second."),
                ("user", "line2"),
            ],
        )

    def test_last_line_without_newline_is_a_line(self):
        self.scene()
        self.assertEqual(self.run_fake("hello", *SESSION, stdin="a\nb").returncode, 0)
        self.assertEqual(self.users(), ["hello", "a", "b"])

    def test_no_prompt_waits_for_a_line(self):
        self.scene(steps=["Hi."], turns=[["Again."]], exit=2)
        proc = self.popen(*SESSION)
        live_tmux.wait(self.logged, what="the fake's log line")
        time.sleep(0.5)
        self.assertIsNone(proc.poll())
        self.assertEqual(self.transcripts(), [])
        out, err = proc.communicate("go\nmore\n", timeout=30)
        self.assertEqual((proc.returncode, out), (2, "Hi.\nAgain.\n"), err)
        self.assertEqual(
            self.convo(),
            [("user", "go"), ("assistant", "Hi."), ("user", "more"), ("assistant", "Again.")],
        )

    def test_eof_before_any_turn(self):
        self.scene(exit=3)
        res = self.run_fake(*SESSION)
        self.assertEqual(res.returncode, 3, res.stderr)
        self.assertEqual(self.transcripts(), [])

    def test_resume_needs_the_transcript(self):
        self.scene()
        res = self.run_fake("--resume", SID)
        self.assertEqual(res.returncode, 1)
        self.assertIn("No conversation found with session ID: " + SID, res.stderr)

    def test_resume_without_a_prompt_appends(self):
        self.scene()
        self.assertEqual(self.run_fake("hello", *SESSION).returncode, 0)
        res = self.run_fake("--resume", SID, stdin="again\n")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.users(), ["hello", "again"])

    def test_hang_and_rate_limit_are_print_mode_only(self):
        self.scene(hang=True, rate_limit={"status": "allowed"})
        res = self.run_fake("hello", *SESSION)
        self.assertEqual((res.returncode, res.stdout), (0, ""), res.stderr)

    def test_output_format_is_print_mode_only(self):
        self.scene()
        res = self.run_fake(*SESSION, "--output-format", "stream-json", "--verbose", "--", "hello")
        self.assertEqual(res.returncode, 2)
        self.assertIn("usage: claude", res.stderr)

    def test_orphaned_it_ends(self):
        self.scene()
        q = shlex.quote
        parent = subprocess.Popen(
            [
                "/bin/sh",
                "-c",
                f"exec 3<&0; {q(sys.executable)} -I {q(FAKE)} --session-id {SID} "
                "<&3 >/dev/null 2>&1 & wait",
            ],
            cwd=self.proj,
            env=self.env,
            stdin=subprocess.PIPE,
        )
        self.addCleanup(self.reap, parent)
        (line,) = live_tmux.wait(self.logged, what="the fake's log line")
        time.sleep(0.5)
        self.assertFalse(gone(line["pid"]))
        parent.kill()
        parent.wait()
        live_tmux.wait(lambda: gone(line["pid"]), what="the orphaned fake to end")

    def test_its_scenario_removed_it_ends(self):
        self.scene()
        proc = self.popen(*SESSION)
        live_tmux.wait(self.logged, what="the fake's log line")
        time.sleep(0.5)
        self.assertIsNone(proc.poll())
        os.remove(self.scenario)
        live_tmux.wait(lambda: proc.poll() is not None, what="the fake to end")
        self.assertEqual(proc.returncode, 1)

    def test_idle_past_the_hang_cap_it_waits_on(self):
        # WDRC-306: idle HANG_CAP s, it exited while a slow test still needed it
        self.scene()
        ticks = itertools.count(step=fake_claude.HANG_CAP + 1)
        polls = []

        def nothing(*_):
            polls.append(None)
            if len(polls) == 5:
                os.remove(self.scenario)
            return [], [], []

        stdin = fake_claude.Stdin(os.getppid(), self.scenario)
        with (
            mock.patch("time.monotonic", side_effect=lambda: next(ticks)),
            mock.patch("select.select", side_effect=nothing),
            self.assertRaises(fake_claude.Stranded),
        ):
            stdin.raw()
        self.assertEqual(len(polls), 5)


class Gate(Case):
    def test_a_session_waits_for_its_gate_after_its_log_line(self):
        gate = os.path.join(self.root, "gate")
        self.scene(steps=["Gated."], gate=gate)
        for n, (args, users) in enumerate(((ARGS, []), (RESUME, ["hi"])), start=1):
            with self.subTest(args[2]):
                proc = self.popen(*args)
                live_tmux.wait(lambda n=n: len(self.logged()) == n, what=f"the fake's log line {n}")
                time.sleep(0.5)
                self.assertIsNone(proc.poll())
                self.assertEqual(self.users() if self.transcripts() else [], users)
                with open(gate, "w"):
                    pass
                out, err = proc.communicate(timeout=30)
                self.assertEqual(proc.returncode, 0, err)
                self.assertEqual(json.loads(out.splitlines()[-1])["result"], "Gated.")
                self.assertEqual(self.users(), [*users, "hi"])
                os.remove(gate)

    def test_the_usage_probe_never_waits(self):
        gate = os.path.join(self.root, "gate")
        self.scene(usage="Current session: 1% used\n", gate=gate)
        self.assertEqual(
            self.run_fake(*ClaudeClient({}).usage_probe().argv[1:]).stdout,
            "Current session: 1% used\n",
        )
        self.assertFalse(os.path.exists(gate))


class Readiness(Case):
    def commands(self) -> list[dict]:
        return [json.loads(line) for line in live_tmux.events(self.commands_file)]

    def setUp(self):
        super().setUp()
        self.commands_file = os.path.join(self.root, "commands.jsonl")

    def write(self, proc: subprocess.Popen, text: str) -> None:
        proc.stdin.write(text)
        proc.stdin.flush()

    def test_a_startup_dialog_shows_before_session_start_which_follows_its_answer(self):
        self.scene(steps=["Hi."], dialog=TRUST)
        events = ("SessionStart", "UserPromptSubmit", "Stop")
        proc = self.popen(*SESSION, "--settings", self.settings(*events), "--", "hello")
        self.assertEqual(proc.stdout.readline(), TRUST + "\n")
        self.assertEqual(self.names(), [])
        out, err = proc.communicate("yes\n", timeout=30)
        self.assertEqual((proc.returncode, out), (0, "Hi.\n"), err)
        self.assertEqual(self.names(), ["SessionStart", "UserPromptSubmit", "Stop"])
        self.assertEqual(self.fired()[0]["source"], "startup")

    def test_session_start_and_local_commands(self):
        self.scene(
            steps=[{"kind": "dialog", "text": TRUST}, "Hi."],
            turns=[["Again."]],
            commands=self.commands_file,
        )
        events = ("SessionStart", "UserPromptSubmit", "Stop")
        proc = self.popen(*SESSION, "--settings", self.settings(*events), "--", "hello")
        self.assertEqual(proc.stdout.readline(), TRUST + "\n")
        time.sleep(fake_claude.READY + 0.2)
        self.write(proc, "/color\n")
        live_tmux.wait(self.commands, what="the command during the dialog")
        self.write(proc, "yes\n")
        live_tmux.wait(
            lambda: self.names().count("SessionStart") == 2, what="SessionStart after the dialog"
        )
        time.sleep(fake_claude.READY + 0.2)
        out, err = proc.communicate("/color\nmore\n", timeout=30)
        self.assertEqual((proc.returncode, out), (0, "Hi.\nAgain.\n"), err)
        self.assertEqual(
            self.names(),
            [
                "SessionStart",
                "UserPromptSubmit",
                "SessionStart",
                "Stop",
                "UserPromptSubmit",
                "Stop",
            ],
        )
        self.assertEqual(self.fired()[0]["source"], "startup")
        self.assertEqual(
            [(c["pid"], c["line"], c["dialog"]) for c in self.commands()],
            [(proc.pid, "/color", True), (proc.pid, "/color", False)],
        )
        self.assertEqual(self.users(), ["hello", "more"])

    def test_a_resume_starts_with_source_resume(self):
        self.scene()
        self.assertEqual(self.run_fake("hello", *SESSION).returncode, 0)
        res = self.run_fake("--resume", SID, "--settings", self.settings("SessionStart"))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual([h["source"] for h in self.fired()], ["resume"])

    def test_a_matcher_searches_the_source(self):
        self.scene()
        tagged = {
            "hooks": {
                "SessionStart": [
                    {"matcher": "^(startup|resume)$", "hooks": [self.tag("start")]},
                    {"matcher": "^clear$", "hooks": [self.tag("clear")]},
                ]
            }
        }
        res = self.run_fake(*SESSION, "--settings", json.dumps(tagged))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(live_tmux.events(self.hooklog), ["start"])

    def test_input_dropped_after_session_start(self):
        self.scene(steps=["Hi."], commands=self.commands_file)
        proc = self.popen(*SESSION, "--settings", self.settings("SessionStart"))
        live_tmux.wait(self.names, what="SessionStart")
        self.write(proc, "/early\nearly\n")
        time.sleep(fake_claude.READY + 0.3)
        out, err = proc.communicate("late\n", timeout=30)
        self.assertEqual((proc.returncode, out), (0, "Hi.\n"), err)
        self.assertEqual(self.users(), ["late"])
        self.assertEqual(self.commands(), [])

    def test_input_before_session_start_is_kept(self):
        self.scene(steps=["Hi."])
        res = self.run_fake(*SESSION, "--settings", self.settings("SessionStart"), stdin="go\n")
        self.assertEqual((res.returncode, res.stdout), (0, "Hi.\n"), res.stderr)
        self.assertEqual(self.users(), ["go"])
