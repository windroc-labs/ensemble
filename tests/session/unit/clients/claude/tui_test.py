"""Claude tui behavior."""

import json
import os
import re
import shlex
from unittest import mock

from ensemble.session.clients.claude import ClaudeClient
from ensemble.session.clients.claude.tui import ClaudeTui, on_hook
from ensemble.session.engine.types import (
    Access,
    Activity,
    Blocked,
    ClientBound,
    ConfigError,
    HookCall,
    HookCallback,
    HookKind,
    Launch,
    LimitHit,
    Reply,
    Spec,
    TurnEnd,
    TurnFailed,
    TurnStarted,
)
from ensemble.utils import docker_home
from tests.session.unit.clients.claude.support import (
    CEILING,
    CONFIG,
    HOOK,
    HOOKS,
    MATCHER,
    NAME,
    NOT_WAITING,
    NOW,
    SID,
    TUI_DEFAULTS,
    TUI_HOOKS,
    TUI_SETTINGS,
    UNSET,
    WAITING,
    WANT_SETTINGS,
    Base,
    ClaudeCommon,
    epoch,
    reference,
    said,
    text,
    tool,
    tui_launch,
)


class TuiArgvTest(Base):
    def test_named_session_keeps_its_name_fresh_and_resumed(self):
        for resume in (False, True):
            with self.subTest(resume=resume):
                argv = self.tui(name="worker-name", resume=resume).argv
                start = argv.index("--name")
                self.assertEqual(argv[start : start + 2], ("--name", "worker-name"))

    def tui(self, prompt="hi", config=CONFIG, *, resume=False, **spec) -> Launch:
        return tui_launch(self.work, prompt, config, resume=resume, **spec)

    def test_a_new_session_with_every_part(self):
        access = Access(
            cwd=self.work,
            dirs=("/d1", "/d 2"),
            commands=("git status", "ls"),
            reads=("/r1", "/r 2/"),
        )
        self.assertEqual(
            self.tui("do it", tier=4, effort="low", access=access).argv,
            (
                "claude",
                "--session-id",
                SID,
                "--model",
                "haiku",
                "--effort",
                "lo",
                "--permission-mode",
                "auto",
                "--setting-sources",
                "user",
                "--settings",
                TUI_SETTINGS,
                "--add-dir",
                "/d1",
                "--add-dir",
                "/d 2",
                "--allowedTools",
                "Bash(git status)",
                "Bash(ls)",
                "Read(//r1/**)",
                "Read(//r 2/**)",
                "Read",
                "Edit(/x)",
                "--",
                "do it",
            ),
        )

    def test_resume_with_none_of_the_optional_parts(self):
        self.assertEqual(
            self.tui(config={}, resume=True).argv,
            (
                "claude",
                "--resume",
                SID,
                "--setting-sources",
                "user",
                "--settings",
                TUI_SETTINGS,
                "--",
                "hi",
            ),
        )

    def test_an_empty_prompt_has_no_separator(self):
        self.assertEqual(self.tui("", config={}).argv[-2:], ("--settings", TUI_SETTINGS))
        self.assertEqual(
            self.tui("", config={"allow": ["Read"]}).argv[-2:], ("--allowedTools", "Read")
        )
        self.assertEqual(self.tui("-p --help", config={}).argv[-2:], ("--", "-p --help"))

    def test_each_hook_runs_the_hook_argv_and_its_label(self):
        self.assertEqual(ClaudeTui.hooks, HOOKS)
        for kind in ClaudeTui.hooks.values():
            self.assertIs(type(kind), HookKind)
        hooks = json.loads(self.tui(config={}).argv[6])["hooks"]
        self.assertEqual(list(hooks), [*HOOKS, "SessionStart"])
        for label, entries in hooks.items():
            with self.subTest(label=label):
                self.assertEqual(shlex.split(entries[0]["hooks"][0]["command"]), [*HOOK, label])

    def test_session_start_hook_outside_hooks(self):
        self.assertEqual(ClaudeTui.opening, ("SessionStart", "/color"))
        self.assertNotIn("SessionStart", ClaudeTui.hooks)
        (entry,) = json.loads(self.tui(config={}).argv[6])["hooks"]["SessionStart"]
        self.assertEqual(entry["matcher"], "^(startup|resume)$")
        for source, hit in (
            ("startup", True),
            ("resume", True),
            ("clear", False),
            ("compact", False),
        ):
            with self.subTest(source=source):
                self.assertEqual(re.search(entry["matcher"], source) is not None, hit)

    def test_the_notification_matcher_takes_only_the_types_waiting_on_the_user(self):
        for kind, hit in ((k, k in WAITING) for k in (*WAITING, *NOT_WAITING)):
            with self.subTest(kind=kind):
                self.assertEqual(re.search(ClaudeTui.MATCHER, kind) is not None, hit)
        self.assertEqual(ClaudeTui.MATCHER, MATCHER)

    def test_a_project_run_and_the_rest_as_headless(self):
        cwd = os.path.join(self.tmp, "cwd")
        os.mkdir(cwd)
        with open(os.path.join(cwd, ".mcp.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        access = Access(cwd=cwd, project=True)
        strict = ("--strict-mcp-config",)
        tui, headless = (
            self.tui(access=access, flags=strict),
            self.launch(access=access, flags=strict),
        )
        self.assertEqual(
            tui.argv[3:9],
            (
                "--permission-mode",
                "auto",
                "--setting-sources",
                "user,project,local",
                "--mcp-config",
                os.path.join(cwd, ".mcp.json"),
            ),
        )
        self.assertEqual(
            (dict(tui.env), dict(headless.env)), ({**TUI_DEFAULTS, "K": "v"}, {**CEILING, "K": "v"})
        )
        self.assertEqual(
            (tui.unset, tui.cwd, tui.transcript, tui.resume),
            (headless.unset, headless.cwd, headless.transcript, headless.resume),
        )

    def test_a_bad_sid_or_tier_is_refused(self):
        self.refused(lambda: self.tui(sid="abc"), "session ids are UUIDs, got 'abc'")
        self.refused(lambda: self.tui(tier=3), "no model for tier 3 in [clients.claude].tiers")

    def test_the_flags_go_before_the_prompt_the_rest_unchanged(self):
        access = Access(cwd=self.work, dirs=("/d1",), commands=("ls",))
        flags = ("--verbose", "--add-dir", "/x")
        for prompt in ("do it", ""):
            with self.subTest(prompt=prompt):
                base = self.tui(prompt, access=access, name=NAME).argv
                cut = len(base) - 2 if prompt else len(base)
                self.assertEqual(
                    self.tui(prompt, access=access, flags=flags, name=NAME).argv,
                    (*base[:cut], *flags, *base[cut:]),
                )


class SessionTuiTest(Base):
    def session(self, prompt="hi", flags=(), config=None, *, resume=False, **spec) -> Launch:
        return tui_launch(
            self.work,
            prompt,
            {} if config is None else config,
            resume=resume,
            name=NAME,
            flags=flags,
            **spec,
        )

    def settings(self, flags) -> tuple[dict, tuple[str, ...]]:
        argv = self.session(flags=flags).argv
        self.assertEqual(
            argv[:8],
            (
                "claude",
                "--session-id",
                SID,
                "--name",
                NAME,
                "--setting-sources",
                "user",
                "--settings",
            ),
        )
        return json.loads(argv[8]), argv[9:]

    def test_new_and_resumed_share_config_access_flags_and_env(self):
        cwd = os.path.join(self.tmp, "cwd")
        os.mkdir(cwd)
        with open(os.path.join(cwd, ".mcp.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        access = Access(
            dirs=("/d1",),
            commands=("ls",),
            reads=("/r1",),
            cwd=cwd,
            project=True,
            env={"K": "access", "S": "s"},
        )
        flags = ("--permission-mode", "plan", "--strict-mcp-config", "--add-dir", "/x")
        for resume, word in ((False, "--session-id"), (True, "--resume")):
            with self.subTest(resume=resume):
                launch = self.session(
                    "do it",
                    flags,
                    config=CONFIG,
                    resume=resume,
                    tier=4,
                    effort="low",
                    access=access,
                    env={"K": "spec"},
                )
                self.assertEqual(
                    launch.argv,
                    (
                        "claude",
                        word,
                        SID,
                        "--model",
                        "haiku",
                        "--effort",
                        "lo",
                        "--name",
                        NAME,
                        "--permission-mode",
                        "auto",
                        "--setting-sources",
                        "user,project,local",
                        "--mcp-config",
                        os.path.join(cwd, ".mcp.json"),
                        "--settings",
                        TUI_SETTINGS,
                        "--add-dir",
                        "/d1",
                        "--allowedTools",
                        "Bash(ls)",
                        "Read(//r1/**)",
                        "Read",
                        "Edit(/x)",
                        *flags,
                        "--",
                        "do it",
                    ),
                )
                self.assertEqual(
                    (dict(launch.env), launch.unset, launch.cwd, launch.transcript, launch.resume),
                    (
                        {**TUI_DEFAULTS, "K": "spec", "S": "s"},
                        UNSET,
                        "",
                        ClaudeCommon({}).transcript(cwd, SID),
                        reference(cwd),
                    ),
                )

    def test_none_of_the_optional_parts(self):
        launch = self.session("")
        self.assertEqual(
            launch.argv,
            (
                "claude",
                "--session-id",
                SID,
                "--name",
                NAME,
                "--setting-sources",
                "user",
                "--settings",
                TUI_SETTINGS,
            ),
        )
        self.assertEqual(
            (dict(launch.env), launch.resume),
            (TUI_DEFAULTS, reference(self.work)),
        )
        self.assertEqual(self.session("-p --help").argv[-2:], ("--", "-p --help"))

    def test_the_hooks_merge_into_the_users_settings_which_move_before_the_rest(self):
        mine = {"type": "command", "command": "mine"}
        own = {"model": "x", "hooks": {"PostToolUse": [mine], "SessionStart": [mine]}}
        merged = {
            "model": "x",
            "hooks": {
                **TUI_HOOKS,
                "PostToolUse": [mine, *TUI_HOOKS["PostToolUse"]],
                "SessionStart": [mine, *TUI_HOOKS["SessionStart"]],
            },
        }
        for flags in (
            ("--verbose", "--settings", json.dumps(own), "--x"),
            ("--verbose", "--settings=" + json.dumps(own), "--x"),
        ):
            with self.subTest(flags=flags):
                self.assertEqual(
                    self.settings(flags),
                    (merged, ("--verbose", "--x", "--", "hi")),
                )
        self.assertEqual(
            self.settings(("--settings", '{"model": "x"}')),
            ({"model": "x", "hooks": TUI_HOOKS}, ("--", "hi")),
        )
        after = ("--", "--settings", "[]")
        self.assertEqual(
            self.settings(after),
            ({"hooks": TUI_HOOKS}, (*after, "--", "hi")),
        )

    def test_the_configs_specs_and_hooks_settings_merge_into_one(self):
        mine = {"type": "command", "command": "mine"}
        config = {
            "flags": [
                "--settings",
                json.dumps({"enabledPlugins": {"a@m": False}, "hooks": {"Stop": [mine]}}),
            ]
        }
        flags = (
            "--settings",
            '{"enabledPlugins": {"b@m": true}, "model": "x"}',
            "--verbose",
            '--settings={"model": "y"}',
        )
        argv = self.session(flags=flags, config=config).argv
        self.assertEqual(argv.count("--settings"), 1)
        at = argv.index("--settings")
        self.assertEqual(
            json.loads(argv[at + 1]),
            {
                "enabledPlugins": {"a@m": False, "b@m": True},
                "model": "y",
                "hooks": {**TUI_HOOKS, "Stop": [mine, *TUI_HOOKS["Stop"]]},
            },
        )
        self.assertEqual(argv[at + 2 :], ("--verbose", "--", "hi"))

    def test_a_malformed_inline_settings_flag_is_refused(self):
        for value in ('{"hooks": null}', '{"hooks": []}', '{"hooks": {"Stop": {}}}', "{x"):
            with self.subTest(value=value):
                self.refused(
                    lambda *, value=value: self.session(flags=("--settings", value)),
                    f"--settings {value!r:.80}: {WANT_SETTINGS}",
                )

    def test_an_empty_or_separator_settings_value_is_a_path_that_is_refused(self):
        limit = "want a regular file of at most 2 MiB"
        for flags, message in (
            (("--settings",), f"{self.work + '/'!r}: {limit}"),
            (("--settings", ""), f"{self.work + '/'!r}: {limit}"),
            (("--settings", "--"), f"{self.work + '/--'!r}: No such file or directory"),
        ):
            with self.subTest(flags=flags):
                self.refused(
                    lambda *, flags=flags: self.session(flags=flags), f"--settings {message}"
                )

    def test_a_settings_file_merges_with_the_hooks(self):
        mine = {"type": "command", "command": "mine"}
        path = os.path.join(self.work, "s.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"model": "x", "hooks": {"Stop": [mine]}}, f)
        for value in (path, "s.json"):
            with self.subTest(value=value):
                self.assertEqual(
                    self.settings(("--settings", value)),
                    (
                        {"model": "x", "hooks": {**TUI_HOOKS, "Stop": [mine, *TUI_HOOKS["Stop"]]}},
                        ("--", "hi"),
                    ),
                )


NATIVE_TRUST = """\
╭──────────────────────────────────────────────────────────────────────────╮
│ Accessing workspace:                                                     │
│                                                                          │
│ /tmp/w                                                                   │
│                                                                          │
│ Quick safety check: Is this a project you created or one you trust?     │
│ (Like your own code, a well-known open source project, or work from     │
│ your team). If not, take a moment to review what's in this folder first. │
│                                                                          │
│ ❯ 1. Yes, I trust this folder                                            │
│   2. No, exit                                                            │
╰──────────────────────────────────────────────────────────────────────────╯
"""


class TuiPayloadTest(Base):
    def setUp(self):
        super().setUp()
        self.run = ClaudeClient({}).tui()
        self.path = os.path.join(self.tmp, "t.jsonl")
        self.run.restore(
            Spec(workdir=self.work, access=Access(cwd=self.work), sid=SID),
            {"transcript": self.path, "start": 0},
        )

    def transcript(self, *records):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("".join(f"{r if isinstance(r, str) else json.dumps(r)}\n" for r in records))

    def test_the_last_assistant_message_first(self):
        self.transcript(said("m1", text("old")))
        self.assertEqual(
            self.run.reply({"last_assistant_message": "new", "transcript_path": self.path}), "new"
        )
        self.assertEqual(
            self.run.reply({"last_assistant_message": "", "transcript_path": self.path}), ""
        )

    def test_else_the_transcripts_last_assistant_message(self):
        self.transcript(
            {"type": "user", "message": {"content": "q"}},
            said("m1", text("old")),
            said("m2", text("first")),
            said("m2", tool("Bash")),
            {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
            said("m2", text("second")),
            said("m3", tool("Read")),
            "not json",
            "[1]",
            "",
        )
        for extra in ({}, {"last_assistant_message": None}, {"last_assistant_message": 1}):
            with self.subTest(extra=extra):
                self.assertEqual(
                    self.run.reply({"transcript_path": self.path, **extra}), "first\nsecond"
                )

    def test_a_record_without_an_id_is_its_own_message(self):
        self.transcript(said(None, text("x")), said(None, text("y")))
        self.assertEqual(self.run.reply({"transcript_path": self.path}), "y")
        self.transcript(
            said("m1", text("x")),
            {"type": "assistant", "message": {"id": 1, "content": [text("y")]}},
        )
        self.assertEqual(self.run.reply({"transcript_path": self.path}), "y")

    def test_wrong_typed_records_are_skipped(self):
        self.transcript(
            said("m1", text("ok")),
            {"type": "assistant", "message": "x"},
            {"type": "assistant", "message": {"content": "x"}},
            {"type": "assistant"},
            said("m2", text(1), {"type": "text"}, "x", 1),
            {"type": "user", "message": said("m3", text("u"))},
        )
        self.assertEqual(self.run.reply({"transcript_path": self.path}), "ok")

    def test_only_the_last_4_mib_is_read(self):
        self.transcript(
            said("m1", text("early")), {"type": "user", "message": {"content": "x" * (5 << 20)}}
        )
        self.assertEqual(self.run.reply({"transcript_path": self.path}), "")
        self.transcript(said("m1", text("x" * (5 << 20))), said("m2", text("late")))
        self.assertEqual(self.run.reply({"transcript_path": self.path}), "late")

    def test_no_readable_transcript_is_empty(self):
        self.transcript(said("m1", text("ok")))
        fifo = os.path.join(self.tmp, "fifo.jsonl")
        os.mkfifo(fifo)
        for path in (
            fifo,
            self.tmp,
            os.path.join(self.tmp, "absent.jsonl"),
            "",
            1,
            None,
            "a\0b",
            "\ud800",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.run.reply({"transcript_path": path}), "")
        self.assertEqual(self.run.reply({}), "")

    def test_pending_activity_and_blocked_reason(self):
        rows = (
            (self.run.pending, {"background_tasks": [1, {}]}, 2),
            (self.run.pending, {"background_tasks": []}, 0),
            (self.run.pending, {}, 0),
            (self.run.pending, {"background_tasks": 3}, 0),
            (self.run.pending, {"background_tasks": "ab"}, 0),
            (self.run.pending, {"background_tasks": {"a": 1}}, 0),
            (self.run.activity, {"tool_name": "Bash"}, "Bash"),
            (self.run.activity, {}, ""),
            (self.run.activity, {"tool_name": 1}, ""),
            (self.run.blocked_reason, {"tool_name": "Bash", "message": "m"}, "Bash"),
            (
                self.run.blocked_reason,
                {"message": "Claude needs your permission"},
                "Claude needs your permission",
            ),
            (self.run.blocked_reason, {"tool_name": 1, "message": "m"}, "m"),
            (self.run.blocked_reason, {"tool_name": "", "message": "m"}, "m"),
            (self.run.blocked_reason, {"tool_name": None, "message": 2}, ""),
            (self.run.blocked_reason, {}, ""),
        )
        for row, (call, payload, want) in enumerate(rows):
            with self.subTest(row=row):
                self.assertEqual(call(payload), want)

    def test_hook_events_by_label(self):
        rows = (
            (
                "Stop",
                {"session_id": SID, "last_assistant_message": "hi", "background_tasks": [1, 2]},
                [ClientBound(SID), TurnEnd("hi", 2)],
            ),
            ("PermissionRequest", {"session_id": SID, "tool_name": "Bash"}, [Blocked("Bash")]),
            (
                "Notification",
                {"session_id": SID, "message": "Claude is waiting"},
                [Blocked("Claude is waiting")],
            ),
            ("PostToolUse", {"session_id": SID, "tool_name": "Edit"}, [Activity("Edit")]),
            ("UserPromptSubmit", {"session_id": SID, "prompt": "x"}, [TurnStarted()]),
            ("SessionStart", {"session_id": SID}, []),
        )
        for label, payload, events in rows:
            with self.subTest(label=label):
                self.assertEqual(self.run.hook_events(label, payload), events)

    def test_a_claude_tui_runs_remote_under_its_session_harness(self):
        self.assertTrue(type(self.run)._remote_owned)

    def test_it_binds_the_sid_it_launched_or_restored_once(self):
        launched = ClaudeClient({}).tui()
        launched.launch(Spec(workdir=self.work, access=Access(cwd=self.work), sid=SID), HOOK)
        for run in (launched, self.run):
            with self.subTest(run=run):
                self.assertEqual(
                    run.hook_events("SessionStart", {"session_id": SID}), [ClientBound(SID)]
                )
                self.assertEqual(run.hook_events("SessionStart", {"session_id": SID}), [])
        self.assertEqual(
            ClaudeClient({}).tui().hook_events("SessionStart", {"session_id": SID}), []
        )

    def test_the_trust_dialog_needs_its_native_rendering(self):
        run, dialog = ClaudeClient({}).tui(), "Quick safety check"
        self.assertEqual(ClaudeTui.dialogs, (dialog,))
        option = "│ ❯ 1. Yes, I trust this folder\n│   2. No, exit\n"
        for screen, shown in (
            (NATIVE_TRUST, True),
            (NATIVE_TRUST.replace("Is this", "is this"), True),
            ("Quick safety check: trust? 1. Yes, I trust this folder 2. No, exit\n", True),
            (
                '  55 TRUST = "Quick safety check: … 1. Yes, I trust this folder 2. No, exit"\n',
                False,
            ),
            ("> see Quick safety check docs\n" + option, False),
            ("│ Quick safety check: Is this a project you trust?\n│ ❯ 1. Yes\n", False),
            ("Quick safety checks pass\n" + option, False),
            ("", False),
        ):
            with self.subTest(screen=screen):
                self.assertIs(run.shows(dialog, screen), shown)
        self.assertTrue(run.shows("Other  dialog", "an Other\n dialog here"))


class StopFailureTest(Base):
    def launched(self):
        run = ClaudeClient({}).tui()
        run.launch(Spec(workdir=self.work, access=Access(cwd=self.work), sid=SID), HOOK)
        run.hook_events("SessionStart", {"session_id": SID})
        return run

    def test_typed_errors(self):
        limit = "You've hit your session limit · resets 3pm (America/Los_Angeles)"
        rows = (
            (
                {"error": "overloaded", "error_details": "529", "last_assistant_message": "x"},
                [TurnFailed("overloaded", "529")],
            ),
            (
                {"error": "authentication_failed", "last_assistant_message": "Please run /login"},
                [TurnFailed("authentication_failed", "Please run /login")],
            ),
            (
                {"error": "billing_error", "error_details": "Credit balance is too low"},
                [TurnFailed("billing_error", "Credit balance is too low")],
            ),
            ({"error": "server_error", "error_details": 5}, [TurnFailed("server_error")]),
            (
                {"error": "rate_limit", "last_assistant_message": limit},
                [LimitHit(epoch("2026-10-11 15:00"))],
            ),
            (
                {"error": "rate_limit", "last_assistant_message": "API Error: Rate limit reached"},
                [LimitHit(None)],
            ),
            ({"error": "rate_limit"}, [LimitHit(None)]),
            ({}, [TurnFailed("unknown")]),
            ({"error": 3, "error_details": ""}, [TurnFailed("unknown")]),
            (
                {"error": "", "last_assistant_message": "API Error"},
                [TurnFailed("unknown", "API Error")],
            ),
        )
        for payload, events in rows:
            with self.subTest(payload=payload), mock.patch("time.time", return_value=NOW):
                self.assertEqual(
                    self.launched().hook_events("StopFailure", {"session_id": SID, **payload}),
                    events,
                )

    def test_a_failure_is_not_sticky(self):
        run = self.launched()
        self.assertEqual(
            run.hook_events("StopFailure", {"session_id": SID, "error": "overloaded"}),
            [TurnFailed("overloaded")],
        )
        self.assertEqual(
            run.hook_events("Stop", {"session_id": SID, "last_assistant_message": "done"}),
            [TurnEnd("done", 0)],
        )


PROMPT = {"type": "user", "message": {"content": "go on"}}

RESULT = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}}


def limited(notice: str, quota=None) -> dict:
    """Claude Code's transcript record of a usage-limit API error."""
    record = {**said(None, text(notice)), "isApiErrorMessage": True, "error": "rate_limit"}
    return record if quota is None else {**record, "quotaLimits": quota}


class TranscriptCase(Base):
    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": os.path.join(self.tmp, "c")})
        patch.start()
        self.addCleanup(patch.stop)
        self.run = ClaudeClient({}).tui()
        spec = Spec(workdir=self.work, access=Access(cwd=self.work), sid=SID)
        self.path = self.run.launch(spec, HOOK).transcript
        self.run.hook_events("SessionStart", {"session_id": SID})

    def launched(self, **spec):
        run = ClaudeClient({}).tui()
        run.launch(
            Spec(**{"workdir": self.work, "access": Access(cwd=self.work), "sid": SID, **spec}),
            HOOK,
        )
        return run

    def append(self, *records, end="\n"):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(end.join(r if isinstance(r, str) else json.dumps(r) for r in records) + end)

    def restored(self):
        run = ClaudeClient({}).tui()
        run.restore(
            Spec(workdir=self.work, access=Access(cwd=self.work), sid=SID),
            {"transcript": self.path, "start": 0},
        )
        run.hook_events("SessionStart", {"session_id": SID})
        return run


class TranscriptTest(TranscriptCase):
    def test_pre_tool_text_is_a_reply_once(self):
        self.append(PROMPT, said("m1", text("start: go")), said("m1", tool("Bash")))
        self.assertEqual(self.run.observe(), [Reply("start: go")])
        self.assertEqual(
            self.run.hook_events("PostToolUse", {"session_id": SID, "tool_name": "Bash"}),
            [Activity("Bash")],
        )
        self.append(RESULT)
        self.assertEqual(self.run.observe(), [])

    def test_each_message_once_then_the_turn_end(self):
        self.append(
            PROMPT,
            said("m1", text("A")),
            said("m1", tool("Bash")),
            RESULT,
            said("m2", text("B")),
            said("m2", tool("Read")),
            RESULT,
        )
        self.assertEqual(self.run.observe(), [Reply("A"), Reply("B")])
        self.append(said("m3", text("C")))
        self.assertEqual(self.run.observe(), [Reply("C")])
        self.assertEqual(
            self.run.hook_events("Stop", {"session_id": SID, "last_assistant_message": "C"}),
            [TurnEnd("C", 0)],
        )
        self.assertEqual(self.run.observe(), [])

    def test_a_late_final_text_is_not_repeated(self):
        self.append(PROMPT, said("m1", text("A")), said("m1", tool("Bash")), RESULT)
        self.assertEqual(self.run.observe(), [Reply("A")])
        self.assertEqual(
            self.run.hook_events("Stop", {"session_id": SID, "last_assistant_message": "C"}),
            [TurnEnd("C", 0)],
        )
        self.append(said("m2", text("B")), said("m2", tool("Read")), RESULT)
        self.assertEqual(self.run.observe(), [Reply("B")])
        self.append(said("m3", text("C")))
        self.assertEqual(self.run.observe(), [])
        self.append(PROMPT, said("m4", text("C")), said("m5", text("D")))
        self.assertEqual(self.run.observe(), [Reply("C"), Reply("D")])

    def test_a_turn_end_already_delivered_leaves_nothing_unseen(self):
        self.append(PROMPT, said("m1", text("C")))
        self.assertEqual(self.run.observe(), [Reply("C")])
        self.assertEqual(
            self.run.hook_events("Stop", {"session_id": SID, "last_assistant_message": "C"}),
            [TurnEnd("C", 0)],
        )
        self.append(said("m2", text("C")))
        self.assertEqual(self.run.observe(), [Reply("C")])

    def test_a_resumed_transcript_starts_at_launch(self):
        self.append(PROMPT, said("m1", text("X")), said("m2", text("Y")))
        size = os.path.getsize(self.path)
        run = self.launched(resume=True)
        self.assertEqual(
            run.hook_callback(),
            HookCallback(
                ClaudeTui.__module__,
                {"transcript": self.path, "start": size, "store": run.common.store, "name": ""},
            ),
        )
        self.assertEqual(run.observe(), [])
        self.append(PROMPT, said("m3", text("Z")))
        self.assertEqual(run.observe(), [Reply("Z")])

    def test_a_restored_cursor_reads_from_its_start(self):
        self.append(PROMPT, said("m1", text("X")))
        size = os.path.getsize(self.path)
        self.append(said("m2", text("Y")))
        run = ClaudeClient({}).tui()
        run.restore(
            Spec(workdir=self.work, access=Access(cwd=self.work), sid=SID),
            {"transcript": self.path, "start": size},
        )
        self.assertEqual(run.observe(), [Reply("Y")])
        self.assertEqual(
            run.hook_callback(),
            HookCallback(
                ClaudeTui.__module__,
                {"transcript": self.path, "start": size, "store": run.common.store, "name": ""},
            ),
        )

    def test_a_bad_cursor_is_refused(self):
        for state in (
            {},
            {"transcript": self.path},
            {"start": 0},
            {"transcript": "t.jsonl", "start": 0},
            {"transcript": 1, "start": 0},
            {"transcript": self.path, "start": -1},
            {"transcript": self.path, "start": True},
            {"transcript": self.path, "start": "0"},
        ):
            with (
                self.subTest(state=state),
                self.assertRaisesRegex(ConfigError, "transcript cursor"),
            ):
                ClaudeClient({}).tui().restore(
                    Spec(workdir=self.work, access=Access(cwd=self.work), sid=SID), state
                )

    def test_a_partial_line_waits_for_its_newline(self):
        self.append(PROMPT)
        line = json.dumps(said("m1", text("A")))
        self.append(line[:10], end="")
        self.assertEqual(self.run.observe(), [])
        self.append(line[10:])
        self.assertEqual(self.run.observe(), [Reply("A")])
        self.assertEqual(self.run.observe(), [])

    def test_a_shorter_transcript_moves_the_cursor_to_its_end(self):
        self.append(PROMPT, said("m1", text("A")))
        self.assertEqual(self.run.observe(), [Reply("A")])
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(json.dumps(PROMPT) + "\n")
        self.assertEqual(self.run.observe(), [])
        self.append(said("m2", text("B")))
        self.assertEqual(self.run.observe(), [Reply("B")])

    def test_no_transcript_is_nothing(self):
        self.assertEqual(self.run.observe(), [])
        os.makedirs(self.path)
        self.assertEqual(self.run.observe(), [])

    def test_only_main_assistant_text_is_a_reply(self):
        self.append(
            PROMPT,
            {**said("s1", text("side")), "isSidechain": True},
            {
                **said("e1", text("API Error: 500")),
                "isApiErrorMessage": True,
                "error": "server_error",
            },
            said("m1", {"type": "thinking", "thinking": "hm"}),
            said("m1", tool("Bash")),
            said("m2", text("  \n ")),
            said("m3", text(1), {"type": "text"}),
            {"type": "assistant", "message": "x"},
            {"type": "assistant", "message": {"content": "x"}},
            {"type": "user", "message": said("m4", text("u"))},
            "not json",
            "[1]",
            said("m5", text("one"), tool("Read"), text("two")),
        )
        self.assertEqual(self.run.observe(), [Reply("one\ntwo")])

    def test_a_limit_record_uses_its_reset(self):
        notice = "You've hit your session limit · resets 3pm (America/Los_Angeles)"
        for quota, resets in (
            ({"status": "rejected", "resetsAt": 1790589600}, 1790589600.0),
            ({"status": "rejected", "resetsAt": 1.5e9}, 1.5e9),
            ({"status": "allowed", "resetsAt": 1790589600}, epoch("2026-10-11 15:00")),
            ({"status": "rejected", "resetsAt": True}, epoch("2026-10-11 15:00")),
            ({"status": "rejected", "resetsAt": 1e999}, epoch("2026-10-11 15:00")),
            ({"status": "rejected"}, epoch("2026-10-11 15:00")),
            ("rejected", epoch("2026-10-11 15:00")),
            (None, epoch("2026-10-11 15:00")),
        ):
            with self.subTest(quota=quota), mock.patch("time.time", return_value=NOW):
                self.append(PROMPT, limited(notice, quota))
                self.assertEqual(self.run.observe(), [LimitHit(resets)])

    def test_one_limit_per_turn_from_the_hook_or_the_record(self):
        notice = "You've hit your weekly limit · resets Oct 15, 2027, 9am (UTC)"
        rate_limit = {"session_id": SID, "error": "rate_limit", "last_assistant_message": notice}
        resets = epoch("2027-10-15 09:00", "UTC")
        self.append(PROMPT, limited(notice))
        self.assertEqual(self.run.observe(), [LimitHit(resets)])
        self.assertEqual(self.run.hook_events("StopFailure", rate_limit), [])
        self.append(PROMPT)
        self.assertEqual(self.run.observe(), [])
        self.assertEqual(self.run.hook_events("StopFailure", rate_limit), [LimitHit(resets)])
        self.append(limited(notice), {**limited(notice), "isSidechain": True})
        self.assertEqual(self.run.observe(), [])
        self.append(PROMPT, {**limited(notice), "isSidechain": True}, limited(notice))
        self.assertEqual(self.run.observe(), [LimitHit(resets)])

    def test_a_replay_keeps_a_late_final_text_unrepeated(self):
        self.append(PROMPT, said("m1", text("A")))
        run = self.restored()
        replayed = run.replay(
            [
                ("UserPromptSubmit", {"session_id": SID}),
                ("Stop", {"session_id": SID, "last_assistant_message": "C"}),
            ]
        )
        self.assertEqual(replayed, [Reply("A"), TurnEnd("C", 0)])
        self.append(said("m2", text("C")))
        self.assertEqual(run.observe(), [])

    def test_a_replay_leaves_no_earlier_turns_text_unseen(self):
        self.append(PROMPT, said("m1", text("C")), PROMPT)
        run = self.restored()
        stop = ("Stop", {"session_id": SID, "last_assistant_message": "C"})
        run.replay(
            [
                ("UserPromptSubmit", {"session_id": SID}),
                stop,
                ("UserPromptSubmit", {"session_id": SID}),
            ]
        )
        self.append(said("m2", text("C")))
        self.assertEqual(run.observe(), [Reply("C")])

    def test_a_replay_of_an_open_turn_leaves_its_newest_text_seen(self):
        self.append(PROMPT, said("m1", text("C")))
        run = self.restored()
        run.replay([("UserPromptSubmit", {"session_id": SID})])
        self.assertEqual(
            run.hook_events("Stop", {"session_id": SID, "last_assistant_message": "C"}),
            [TurnEnd("C", 0)],
        )
        self.append(said("m2", text("C")))
        self.assertEqual(run.observe(), [Reply("C")])

    def test_after_a_replay_the_latest_turns_limit_is_given_live_once(self):
        notice = "You've hit your weekly limit · resets Oct 15, 2027, 9am (UTC)"
        rate_limit = (
            "StopFailure",
            {"session_id": SID, "error": "rate_limit", "last_assistant_message": notice},
        )
        resets = epoch("2027-10-15 09:00", "UTC")
        self.append(PROMPT, limited(notice), PROMPT)
        run = self.restored()
        run.replay(
            [
                ("UserPromptSubmit", {"session_id": SID}),
                rate_limit,
                ("UserPromptSubmit", {"session_id": SID}),
            ]
        )
        self.append(limited(notice))
        self.assertEqual(run.observe(), [LimitHit(resets)])
        self.assertEqual(run.hook_events(*rate_limit), [])
        run = self.restored()
        run.replay(
            [
                ("UserPromptSubmit", {"session_id": SID}),
                rate_limit,
                ("UserPromptSubmit", {"session_id": SID}),
            ]
        )
        self.assertEqual(run.hook_events(*rate_limit), [LimitHit(resets)])
        self.assertEqual(run.observe(), [])


WEEKLY = "You've hit your weekly limit · resets Oct 15, 2027, 9am (UTC)"
STOP = ("Stop", {"session_id": SID, "last_assistant_message": "Done."})
RATE_LIMIT = (
    "StopFailure",
    {"session_id": SID, "error": "rate_limit", "last_assistant_message": WEEKLY},
)


class JoinedInputTest(TranscriptCase):
    """Claude runs UserPromptSubmit for input it joins to its open turn (a send, a background
    task's notice): no new turn, until the turn ends by a hook or on a usage limit.
    """

    def prompt(self, run=None):
        return (run or self.run).hook_events("UserPromptSubmit", {"session_id": SID, "prompt": "x"})

    def limit(self):
        self.append(PROMPT, limited(WEEKLY))
        self.assertEqual(self.run.observe(), [LimitHit(epoch("2027-10-15 09:00", "UTC"))])

    def test_a_prompt_while_the_turn_is_open_joins_it(self):
        self.assertEqual(self.prompt(), [TurnStarted()])
        self.assertEqual(self.prompt(), [])
        self.assertEqual(self.prompt(), [])
        self.assertEqual(self.run.hook_events(*STOP), [TurnEnd("Done.", 0)])
        self.assertEqual(self.prompt(), [TurnStarted()])
        self.assertEqual(
            self.run.hook_events(*RATE_LIMIT), [LimitHit(epoch("2027-10-15 09:00", "UTC"))]
        )
        self.assertEqual(self.prompt(), [TurnStarted()])

    def test_a_first_prompt_binds_the_session_and_starts_its_turn(self):
        run = self.launched()
        self.assertEqual(self.prompt(run), [ClientBound(SID), TurnStarted()])
        self.assertEqual(self.prompt(run), [])

    def test_a_limit_only_the_transcript_shows_ends_the_open_turn(self):
        self.assertEqual(self.prompt(), [TurnStarted()])
        self.limit()
        self.assertEqual(self.prompt(), [TurnStarted()])
        self.assertEqual(self.prompt(), [])

    def resumed_after_a_limit_read_before_its_prompt(self, *, user_first: bool) -> None:
        """observe() runs before a batch's hooks: a limit can be read before its own prompt."""
        self.assertEqual(self.prompt(), [TurnStarted()])
        self.assertEqual(self.run.hook_events(*STOP), [TurnEnd("Done.", 0)])
        self.limit()
        self.assertEqual(self.prompt(), [TurnStarted()])
        if user_first:
            self.append(PROMPT)
            self.assertEqual(self.run.observe(), [])
        self.assertEqual(self.prompt(), [TurnStarted()])
        self.assertEqual(self.prompt(), [])

    def test_a_limit_read_before_its_prompt_ends_that_turn(self):
        self.resumed_after_a_limit_read_before_its_prompt(user_first=False)

    def test_a_limit_read_before_its_prompt_ends_that_turn_the_resume_record_first(self):
        self.resumed_after_a_limit_read_before_its_prompt(user_first=True)

    def test_a_limit_in_the_transcript_and_its_stop_failure_ends_the_turn_once(self):
        self.assertEqual(self.prompt(), [TurnStarted()])
        self.limit()
        self.assertEqual(self.run.hook_events(*RATE_LIMIT), [])
        self.assertEqual(self.prompt(), [TurnStarted()])
        self.assertEqual(self.prompt(), [])

    def test_a_replay_keeps_a_limit_of_its_newest_turn_only(self):
        ups = ("UserPromptSubmit", {"session_id": SID})
        self.append(PROMPT, limited(WEEKLY), PROMPT, said("m1", text("Done.")), PROMPT)
        run = self.restored()
        run.replay([ups, RATE_LIMIT, ups, STOP, ups])
        self.assertEqual(self.prompt(run), [])
        self.append(limited(WEEKLY))
        run = self.restored()
        run.replay([ups, RATE_LIMIT, ups, STOP, ups])
        self.assertEqual(self.prompt(run), [TurnStarted()])
        self.assertEqual(self.prompt(run), [])


NEXT = "1f8fad5b-d9cb-469f-a165-70867728950e"
LAST = "2f8fad5b-d9cb-469f-a165-70867728950e"
CUT = "2026-10-15T12:00:00Z"


class DockerHomeTest(TranscriptCase):
    """A transcript under a Docker session's home, which its container can write."""

    def setUp(self):
        super().setUp()
        home = docker_home("w", "claude")
        self.enterContext(mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": home}))
        self.run = self.launched()
        self.run.hook_events("SessionStart", {"session_id": SID})
        self.path = self.run.transcript
        self.outside = os.path.join(self.tmp, "outside.jsonl")
        with open(self.outside, "w", encoding="utf-8") as f:
            f.write(json.dumps(said("m1", text("host secret"))) + "\n")

    def test_a_regular_transcript_there_is_read(self):
        self.assertTrue(self.path.startswith(docker_home("w", "claude") + os.sep))
        self.append(PROMPT, said("m1", text("A")))
        self.assertEqual(self.run.observe(), [Reply("A")])

    def test_docker_home_transcript_symlink_reads_nothing(self):
        os.makedirs(os.path.dirname(self.path))
        os.symlink(self.outside, self.path)
        self.assertEqual(self.run.observe(), [])
        self.assertEqual(self.run.reply({"transcript_path": self.path}), "")

    def test_a_linked_dir_above_the_transcript_reads_nothing(self):
        projects = os.path.dirname(os.path.dirname(self.path))
        os.makedirs(os.path.dirname(projects))
        target = os.path.join(self.tmp, "linked")
        os.makedirs(os.path.join(target, os.path.basename(os.path.dirname(self.path))))
        os.symlink(
            self.outside,
            os.path.join(
                target, os.path.basename(os.path.dirname(self.path)), os.path.basename(self.path)
            ),
        )
        os.symlink(target, projects)
        self.assertEqual(self.run.observe(), [])


class ContinuationTest(TranscriptCase):
    def marker(self, source=SID, target=NEXT):
        return {
            "type": "continued-in",
            "sessionId": source,
            "continuedInSessionId": target,
            "timestamp": CUT,
        }

    def target(self, sid=NEXT, *records):
        path = os.path.join(
            os.environ["CLAUDE_CONFIG_DIR"], "projects", "elsewhere", f"{sid}.jsonl"
        )
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as out:
            out.write("".join(json.dumps(r) + "\n" for r in records))
        return path

    def test_callback_rejects_foreign_payload_before_dispatch(self):
        for payload in ({}, {"session_id": NEXT}, {"session_id": []}):
            with self.subTest(payload=payload):
                call = HookCall(
                    label="Stop",
                    payload=payload,
                    sid=SID,
                    workdir=self.work,
                    events=None,
                    state=self.run.hook_callback().state,
                )
                self.assertIsNone(on_hook(call))

    def test_callback_accepts_a_missing_continuation_target(self):
        self.append(self.marker())
        for sid in (SID, NEXT):
            with self.subTest(sid=sid):
                payload = {"session_id": sid, "last_assistant_message": "done"}
                call = HookCall(
                    label="Stop",
                    payload=payload,
                    sid=SID,
                    workdir=self.work,
                    events=None,
                    state=self.run.hook_callback().state,
                )
                self.assertEqual(on_hook(call), payload)

    def test_callback_without_a_transcript_cannot_dispatch(self):
        for state in ({}, {"transcript": "relative"}):
            with self.subTest(state=state):
                self.assertIsNone(
                    on_hook(
                        HookCall(
                            label="Stop",
                            payload={"session_id": SID},
                            sid=SID,
                            workdir=self.work,
                            events=None,
                            state=state,
                        )
                    )
                )

    def test_resumed_callback_keeps_the_chain(self):
        self.append(self.marker())
        self.target(NEXT, said("old", text("past")))
        resumed = self.launched(resume=True)
        payload = {"session_id": NEXT}
        self.assertEqual(
            on_hook(
                HookCall(
                    label="SessionStart",
                    payload=payload,
                    sid=SID,
                    workdir=self.work,
                    events=None,
                    state=resumed.hook_callback().state,
                )
            ),
            payload,
        )

    def test_delayed_target_keeps_old_file_readable(self):
        self.append(self.marker(), said("m1", text("old tail")))
        self.assertEqual(self.run.observe(), [Reply("old tail")])
        self.assertEqual(
            self.run.hook_events("Stop", {"session_id": NEXT, "last_assistant_message": "done"}),
            [TurnEnd("done", 0)],
        )
        self.append(said("m2", text("still old")))
        self.assertEqual(self.run.observe(), [Reply("still old")])
        self.target(
            NEXT,
            {**said("m1", text("copied")), "timestamp": CUT},
            {**PROMPT, "timestamp": CUT},
            said("m3", text("no timestamp")),
            {**said("m4", text("new")), "timestamp": "2026-10-15T12:00:01Z"},
        )
        self.assertEqual(self.run.observe(), [Reply("no timestamp"), Reply("new")])
        self.assertEqual(self.run.observe(), [])

    def test_resumed_cursor_retries_an_earlier_missing_target(self):
        self.append(self.marker())
        resumed = self.launched(resume=True)
        self.assertEqual(resumed.observe(), [])
        self.target(
            NEXT, {**said("old", text("copied")), "timestamp": CUT}, said("new", text("arrived"))
        )
        self.assertEqual(resumed.observe(), [Reply("arrived")])
        self.assertEqual(resumed.observe(), [])

    def test_two_hops_replay_once_from_original_cursor(self):
        self.append(self.marker())
        self.target(NEXT, said("m1", text("first")), self.marker(NEXT, LAST))
        self.target(
            LAST, {**said("m1", text("first")), "timestamp": CUT}, said("m2", text("second"))
        )
        state = self.run.hook_callback().state
        self.assertEqual(self.run.observe(), [Reply("first"), Reply("second")])
        self.assertEqual(self.run.hook_callback().state, state)
        restored = self.restored()
        self.assertEqual(restored.observe(), [Reply("first"), Reply("second")])
        self.assertEqual(restored.observe(), [])
        self.assertEqual(
            restored.hook_events("Stop", {"session_id": LAST, "last_assistant_message": "second"}),
            [TurnEnd("second", 0)],
        )

    def test_resumed_latest_cursor_restores_original_chain_ids(self):
        self.append(self.marker())
        path = self.target(NEXT, said("old", text("past")))
        resumed = self.launched(resume=True)
        self.assertEqual(resumed.transcript, path)
        state = resumed.hook_callback().state
        restored = ClaudeClient({}).tui()
        restored.restore(Spec(workdir=self.work, access=Access(cwd=self.work), sid=SID), state)
        self.assertEqual(restored.observe(), [])
        for sid in (SID, NEXT):
            self.assertEqual(
                restored.hook_events("UserPromptSubmit", {"session_id": sid}),
                ([ClientBound(SID), TurnStarted()] if sid == SID else [TurnStarted()]),
            )
            stop = {"session_id": sid, "last_assistant_message": ""}
            self.assertEqual(restored.hook_events("Stop", stop), [TurnEnd("", 0)])

    def test_foreign_hooks_have_no_replay_effect(self):
        self.append(said("m1", text("seen")))
        restored = self.restored()
        self.assertEqual(
            restored.replay(
                [
                    ("Stop", {"session_id": SID, "last_assistant_message": "late"}),
                    ("UserPromptSubmit", {"session_id": NEXT}),
                    ("StopFailure", {"session_id": NEXT, "error": "rate_limit"}),
                ]
            ),
            [Reply("seen"), TurnEnd("late", 0)],
        )
        self.append(said("m2", text("late")))
        self.assertEqual(restored.observe(), [])
        for payload in ({}, {"session_id": NEXT}, {"session_id": None}, {"session_id": []}):
            self.assertEqual(restored.hook_events("Stop", payload), [])
