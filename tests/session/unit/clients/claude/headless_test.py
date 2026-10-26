"""Claude headless behavior."""

import json
import os
import time
from unittest import mock

from ensemble.session import Spec
from ensemble.session.clients.claude import ClaudeClient
from ensemble.session.engine.types import (
    Access,
    Activity,
    ClientBound,
    LimitHit,
    Reply,
    TurnEnd,
    TurnFailed,
    TurnStarted,
)
from ensemble.utils import docker_home
from tests.session.case import Case
from tests.session.unit.clients.claude.support import (
    CONFIG,
    INIT,
    NOW,
    SID,
    WANT_SETTINGS,
    Base,
    ClaudeCommon,
    assistant,
    epoch,
    rejected,
    stream_line,
    text,
    tool,
    tui_launch,
)

CLAUDE = ("--permission-mode", "auto", "--strict-mcp-config")


class ArgvTest(Base):
    def test_a_new_session_with_every_part(self):
        access = Access(
            cwd=self.work,
            dirs=("/d1", "/d 2"),
            commands=("git status", "ls"),
            reads=("/r1", "/r 2/"),
        )
        self.assertEqual(
            self.launch("do it", tier=4, effort="low", access=access).argv,
            (
                "claude",
                "-p",
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
                "--output-format",
                "stream-json",
                "--verbose",
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

    def test_resume_continues_the_sid(self):
        self.assertEqual(
            self.launch(config={}, resume=True).argv[:4], ("claude", "-p", "--resume", SID)
        )

    def test_none_of_the_optional_parts(self):
        self.assertEqual(
            self.launch(config={}).argv,
            (
                "claude",
                "-p",
                "--session-id",
                SID,
                "--setting-sources",
                "user",
                "--output-format",
                "stream-json",
                "--verbose",
                "--",
                "hi",
            ),
        )

    def test_allowed_tools_from_commands_or_allow_alone(self):
        self.assertEqual(
            self.launch(config={}, access=Access(cwd=self.work, commands=("ls",))).argv[-4:],
            ("--allowedTools", "Bash(ls)", "--", "hi"),
        )
        self.assertEqual(
            self.launch(config={"allow": ["Read"]}).argv[-4:],
            ("--allowedTools", "Read", "--", "hi"),
        )

    def test_the_configs_and_specs_settings_reach_claude_as_one(self):
        config = {"flags": ["--settings", '{"enabledPlugins": {"a@m": false}, "model": "x"}']}
        flags = ("--verbose", "--settings", '{"enabledPlugins": {"b@m": true}, "model": "y"}')
        argv = self.launch(config=config, flags=flags).argv
        self.assertEqual(argv.count("--settings"), 1)
        at = argv.index("--settings")
        self.assertEqual(
            json.loads(argv[at + 1]), {"enabledPlugins": {"a@m": False, "b@m": True}, "model": "y"}
        )
        self.assertEqual(argv[-3:], ("--verbose", "--", "hi"))
        self.assertNotIn("--settings", self.launch(config={}).argv)

    def test_settings_files_and_inline_json_reach_claude_as_one(self):
        cwd = os.path.join(self.tmp, "cwd")
        os.mkdir(cwd)
        own = os.path.join(self.tmp, "own.json")
        with open(own, "w", encoding="utf-8") as f:
            json.dump({"enabledPlugins": {"a@m": False}, "model": "x"}, f)
        with open(os.path.join(cwd, "run.json"), "w", encoding="utf-8") as f:
            json.dump({"enabledPlugins": {"b@m": True}}, f)
        flags = ("--settings", "run.json", "--settings", '{"model": "y"}')
        argv = self.launch(
            config={"flags": ["--settings", own]}, access=Access(cwd=cwd), flags=flags
        ).argv
        self.assertEqual(argv.count("--settings"), 1)
        self.assertEqual(
            json.loads(argv[argv.index("--settings") + 1]),
            {"enabledPlugins": {"a@m": False, "b@m": True}, "model": "y"},
        )

    def test_a_settings_path_claude_could_not_load_is_refused_by_its_full_path(self):
        deep = os.path.join(self.work, "d" * 90)
        os.mkdir(deep)
        big = os.path.join(deep, "big.json")
        with open(big, "wb") as f:
            f.truncate(2 * 1024 * 1024 + 1)
        listed = os.path.join(deep, "list.json")
        with open(listed, "w", encoding="utf-8") as f:
            f.write("[]")
        limit = "want a regular file of at most 2 MiB"
        far = os.path.join(deep, "missing.json")
        for value, message in (
            (deep, limit),
            (big, limit),
            (listed, WANT_SETTINGS),
            ("missing.json", "No such file or directory"),
            (far, "No such file or directory"),
        ):
            with self.subTest(value=value):
                self.refused(
                    lambda *, value=value: self.launch(config={}, flags=("--settings", value)),
                    f"--settings {os.path.join(self.work, value)!r}: {message}",
                )

    def test_a_prompt_starting_with_a_dash_stays_last_after_the_separator(self):
        self.assertEqual(self.launch("-p --help", config={}).argv[-2:], ("--", "-p --help"))

    def test_a_project_run_loads_project_settings_and_cwds_mcp_file(self):
        cwd = os.path.join(self.tmp, "cwd")
        os.mkdir(cwd)
        argv = self.launch(config={}, access=Access(cwd=cwd, project=True)).argv
        self.assertEqual(argv[4:7], ("--setting-sources", "user,project,local", "--output-format"))
        for parent in (cwd, self.work):
            with open(os.path.join(parent, ".mcp.json"), "w", encoding="utf-8") as f:
                f.write("{}")
        strict = ("--strict-mcp-config",)
        self.assertNotIn("--mcp-config", self.launch(access=Access(cwd=cwd, project=True)).argv)
        argv = self.launch(config={}, access=Access(cwd=cwd, project=True), flags=strict).argv
        self.assertEqual(
            argv[4:9],
            (
                "--setting-sources",
                "user,project,local",
                "--mcp-config",
                os.path.join(cwd, ".mcp.json"),
                "--output-format",
            ),
        )
        self.assertEqual(
            self.launch(config={}, access=Access(cwd=self.work, project=True), flags=strict).argv[
                6:8
            ],
            ("--mcp-config", os.path.join(self.work, ".mcp.json")),
        )
        self.assertNotIn(
            "--mcp-config", self.launch(config={}, access=Access(cwd=cwd), flags=strict).argv
        )

    def test_an_mcp_path_that_is_no_file_is_skipped(self):
        os.mkdir(os.path.join(self.work, ".mcp.json"))
        self.assertNotIn(
            "--mcp-config",
            self.launch(
                config={},
                access=Access(cwd=self.work, project=True),
                flags=("--strict-mcp-config",),
            ).argv,
        )

    def test_a_sid_that_is_no_canonical_uuid_is_refused(self):
        for bad in (
            "abc",
            SID.upper(),
            "{" + SID + "}",
            SID.replace("-", ""),
            SID + "0",
            " " + SID,
        ):
            with self.subTest(bad=bad):
                self.refused(
                    lambda *, bad=bad: self.launch(sid=bad), f"session ids are UUIDs, got {bad!r}"
                )
        self.refused(
            lambda: ClaudeCommon.uuid(SID + "\n"),
            f"claude session ids are UUIDs, got {SID + chr(10)!r}",
        )
        self.refused(lambda: ClaudeCommon.uuid(None), "claude session ids are UUIDs, got None")
        self.assertEqual(ClaudeCommon.uuid(SID), SID)


class ParseTest(Base):
    def test_error_result_and_permission_denial_cannot_be_success(self):
        for fields, kind in (
            (
                {"subtype": "error_during_execution", "is_error": True, "errors": ["overloaded"]},
                "error_during_execution",
            ),
            (
                {
                    "subtype": "success",
                    "permission_denials": [{"tool_name": "Bash"}],
                    "result": "denied",
                },
                "permission_required",
            ),
        ):
            with self.subTest(fields=fields):
                found = self.parse({"type": "result", **fields})
                self.assertEqual(len(found), 1)
                self.assertIsInstance(found[0], TurnFailed)
                self.assertEqual(found[0].error, kind)

    def test_native_initialization_starts_one_accepted_headless_turn(self):
        self.assertEqual(self.parse(INIT), [ClientBound(SID), TurnStarted()])
        self.assertFalse(any(isinstance(e, TurnStarted) for e in self.parse(INIT)))

    def setUp(self):
        super().setUp()
        self.run = ClaudeClient({}).headless()
        self.run.launch(Spec(workdir=self.work, access=Access(cwd=self.work), sid=SID, prompt="hi"))

    def parse(self, *records):
        return [event for record in records for event in self.run.parse(record)]

    def test_a_session_in_order(self):
        lines = [
            stream_line(INIT),
            "not json",
            stream_line(assistant(text("one"))),
            stream_line(assistant(text("two"))),
            stream_line({"type": "user", "message": {"content": [{"type": "tool_result"}]}}),
            "[1]",
            stream_line(assistant(text("three"))),
            stream_line({"type": "result", "subtype": "success", "result": "three"}),
        ]
        self.assertEqual(
            list(self.run.events(lines)),
            [
                ClientBound(SID),
                TurnStarted(),
                Reply("one"),
                Reply("two"),
                Reply("three"),
                TurnEnd("three", 0),
            ],
        )

    def test_init_binds_the_sid_it_launched(self):
        self.assertEqual(self.parse(INIT), [ClientBound(SID), TurnStarted()])
        self.assertEqual(self.parse({**INIT, "session_id": "other"}), [ClientBound(SID)])
        self.assertEqual(list(ClaudeClient({}).headless().parse(INIT)), [])
        for record in (
            {**INIT, "subtype": "other"},
            {**INIT, "session_id": ""},
            {**INIT, "session_id": 1},
            {"type": "system", "subtype": "init"},
        ):
            with self.subTest(record=record):
                self.assertEqual(self.parse(record), [])

    def test_text_blocks_joined_blank_none(self):
        self.assertEqual(self.parse(assistant(text("a"), text("b"))), [Reply("a\nb")])
        self.assertEqual(self.parse(assistant(text(" \n"), text(""))), [])
        self.assertEqual(self.parse(assistant()), [])

    def test_tool_use_is_activity_after_the_reply(self):
        self.assertEqual(
            self.parse(assistant(tool("Bash"), text("x"), tool("Read"))),
            [Reply("x"), Activity("Bash"), Activity("Read")],
        )

    def test_result_is_the_turn_end(self):
        self.assertEqual(
            self.parse({"type": "result", "subtype": "success", "result": "done"}),
            [TurnEnd("done", 0)],
        )
        self.assertEqual(
            self.parse({"type": "result", "result": None}, {"type": "result"}),
            [TurnFailed("incomplete_result"), TurnFailed("incomplete_result")],
        )

    def test_other_records_nothing(self):
        self.assertEqual(
            self.parse({"type": "user"}, {"type": "stream_event"}, {}, {"type": 1}), []
        )

    def test_wrong_typed_fields_are_absent(self):
        rows = (
            ({"type": "assistant", "message": "x"}, []),
            ({"type": "assistant", "message": {"content": "x"}}, []),
            ({"type": "assistant"}, []),
            (assistant("x", 1, None, text("y")), [Reply("y")]),
            (assistant(text(1), {"type": "text"}), []),
            (assistant({"type": "tool_use", "name": 1}), [Activity("")]),
            (assistant({"type": "tool_use"}), [Activity("")]),
            ({"type": "rate_limit_event", "rate_limit_info": "rejected"}, []),
            ({"type": "rate_limit_event"}, []),
        )
        for record, events in rows:
            with self.subTest(record=record):
                self.assertEqual(list(ClaudeClient({}).headless().parse(record)), events)

    def test_a_rejected_rate_limit_event_is_a_limit_hit(self):
        self.assertEqual(self.parse(rejected(1700000000)), [LimitHit(1700000000.0)])
        for record in (rejected(status="allowed"), rejected(status="allowed_warning")):
            with self.subTest(record=record):
                self.assertEqual(list(ClaudeClient({}).headless().parse(record)), [])
        for resets in (None, True, "1700000000", 1e999):
            with self.subTest(resets=resets):
                self.assertEqual(
                    list(ClaudeClient({}).headless().parse(rejected(resets))),
                    [LimitHit(None)],
                )

    def test_an_assistant_rate_limit_error_is_one_limit_hit_with_the_remembered_reset(self):
        self.assertEqual(
            self.parse(rejected(5), assistant(text("limit"), error="rate_limit"), rejected(6)),
            [LimitHit(5.0), Reply("limit")],
        )
        run = ClaudeClient({}).headless()
        self.assertEqual(
            [
                e
                for r in (
                    assistant(text("limit"), error="rate_limit"),
                    assistant(error="rate_limit"),
                    rejected(7),
                )
                for e in run.parse(r)
            ],
            [LimitHit(None), Reply("limit")],
        )
        self.assertEqual(list(ClaudeClient({}).headless().parse(assistant(error="other"))), [])

    def test_each_run_sends_its_own(self):
        client = ClaudeClient({})
        first, second = client.headless(), client.headless()
        self.assertIsNot(first, second)
        self.assertEqual(list(first.parse(rejected(1))), [LimitHit(1.0)])
        self.assertEqual(list(second.parse(assistant(error="rate_limit"))), [LimitHit(None)])


class LimitTest(Case):
    """A rate_limit StopFailure's reset, read from its rendered notice; never from the screen."""

    def limit(self, message, now=NOW):
        payload = {"error": "rate_limit", "last_assistant_message": message}
        with mock.patch("time.time", return_value=now):
            return ClaudeClient({}).tui().failure(payload)

    def check(self, rows):
        for message, resets in rows:
            with self.subTest(message=message):
                self.assertEqual(self.limit(message), [LimitHit(resets)])

    def test_each_limit_notice_and_its_reset_or_none(self):
        week = "You've hit your weekly limit · resets {} (America/Los_Angeles)"
        session = "You've hit your session limit · resets 3pm (America/Los_Angeles)"
        self.check(
            (
                (
                    "You've hit your session limit · resets 3pm (America/Los_Angeles)",
                    epoch("2026-10-11 15:00"),
                ),
                (
                    "You've hit your session limit · resets 9:30am (America/Los_Angeles)",
                    epoch("2026-10-12 09:30"),
                ),
                (
                    (
                        "You've hit your weekly limit · resets Oct 15, 9am (America/Los_Angeles) · progress saved"
                    ),
                    epoch("2026-10-15 09:00"),
                ),
                (
                    "You've hit your Opus limit · resets Jan 2, 2027, 12am (Europe/London)",
                    epoch("2027-01-02 00:00", "Europe/London"),
                ),
                (
                    "You've hit your usage limit · resets 12pm (UTC)",
                    epoch("2026-10-12 12:00", "UTC"),
                ),
                (
                    (
                        "You've hit your org's monthly spend limit · your session limit resets 4pm (Asia/Tokyo)"
                    ),
                    epoch("2026-10-12 16:00", "Asia/Tokyo"),
                ),
                (
                    "You've hit your limit · resets 11pm (Etc/GMT+5)",
                    epoch("2026-10-11 23:00", "Etc/GMT+5"),
                ),
                (
                    "Usage limit reached · resets 3pm (America/New_York)",
                    epoch("2026-10-11 15:00", "America/New_York"),
                ),
                (
                    (
                        "> go on\n\n  ⎿  You've hit your\n   session  limit ·\n resets 3pm\n  (America/Los_Angeles)\n"
                    ),
                    epoch("2026-10-11 15:00"),
                ),
                (week.format("Monday 9am"), epoch("2026-10-12 09:00")),
                (week.format("Sun 11am"), epoch("2026-10-11 11:00")),
                (week.format("Sunday, 9am"), epoch("2026-10-18 09:00")),
                (week.format("sat 9am"), epoch("2026-10-17 09:00")),
                (
                    (
                        f"{session} … You've hit your weekly limit · resets Oct 15, 9am (America/Los_Angeles)"
                    ),
                    epoch("2026-10-15 09:00"),
                ),
                (f"{session} You've hit your fast limit · resets in 5m", epoch("2026-10-11 15:00")),
                ("You've hit your session limit", None),
                ("Usage limit reached · continuing automatically in 2h · esc to cancel", None),
                ("You've hit your session limit · resets soon", None),
                ("You've hit your session limit · resets 13pm (UTC)", None),
                ("You've hit your session limit · resets 0am (UTC)", None),
                ("You've hit your session limit · resets 3:75pm (UTC)", None),
                ("You've hit your weekly limit · resets Feb 30, 3pm (UTC)", None),
                ("You've hit your weekly limit · resets Monday 15, 3pm (UTC)", None),
                ("You've hit your weekly limit · resets Smarch 3, 3pm (UTC)", None),
                ("You've hit your weekly limit · resets Funday 3pm (UTC)", None),
                ("You've hit your weekly limit · resets Oct 3pm (UTC)", None),
                *(
                    (message, None)
                    for message in (
                        "",
                        "Welcome back",
                        "You've hit your fast limit · resets in 5m",
                        "You've used 90% of your session limit · resets 3pm (America/Los_Angeles)",
                        "Approaching weekly limit",
                        "Your usage limit has reset",
                        "usage limit reached",
                    )
                ),
            )
        )

    def test_a_year_less_date_over_a_day_past_is_next_years(self):
        week = "You've hit your weekly limit · resets {} (America/Los_Angeles)"
        for now, when, resets in (
            ("2026-12-30 10:00", "Jan 2, 9am", "2027-01-02 09:00"),
            ("2026-01-01 10:00", "Jan 2, 9am", "2026-01-02 09:00"),
            ("2026-12-30 10:00", "Dec 30, 9am", "2026-12-30 09:00"),
            ("2026-12-30 10:00", "Dec 29, 9am", "2027-12-29 09:00"),
            ("2026-12-30 10:00", "Jan 2, 2026, 9am", "2026-01-02 09:00"),
        ):
            with self.subTest(now=now, when=when):
                self.assertEqual(
                    self.limit(week.format(when), epoch(now)), [LimitHit(epoch(resets))]
                )

    def test_no_known_zone_is_local_time(self):
        self.addCleanup(time.tzset)
        patch = mock.patch.dict(os.environ, {"TZ": "Asia/Tokyo"})
        patch.start()
        self.addCleanup(patch.stop)
        time.tzset()
        today = epoch("2026-10-12 15:00", "Asia/Tokyo")
        self.check(
            (
                ("You've hit your session limit · resets 3pm", today),
                ("You've hit your session limit · resets 3pm (Mars/Olympus)", today),
                ("You've hit your session limit · resets 3pm (America)", today),
                ("You've hit your session limit · resets 3pm (../../etc/passwd)", today),
                ("You've hit your session limit · resets 3pm ()", today),
                ("You've hit your session limit · resets 3pm (A/B/C/D)", today),
                (
                    "You've hit your weekly limit · resets Oct 15, 9am",
                    epoch("2026-10-15 09:00", "Asia/Tokyo"),
                ),
            )
        )

    def test_no_screen_is_a_limit(self):
        for screen in (
            "You've hit your session limit · resets 3pm (America/Los_Angeles)",
            "> go on\n\n  ⎿  You've hit your\n   session  limit ·\n resets 3pm\n",
            "Usage limit reached · continuing automatically in 2h · esc to cancel",
            "> Documentation example: You've hit your weekly limit",
            "  12 + You've hit your session limit · resets 3pm (UTC)",
        ):
            with self.subTest(screen=screen):
                self.assertIsNone(ClaudeClient({}).tui().limit(screen))


class SuppliedPolicy(Base):
    def test_a_policy_reaches_both_client_launches(self):
        flags = CLAUDE
        client = ClaudeClient({})
        spec = Spec(
            workdir=str(self.work),
            access=Access(cwd=str(self.work)),
            sid=SID,
            flags=flags,
            prompt="hi",
        )
        launch = client.headless().launch(spec)
        start = launch.argv.index(flags[0])
        self.assertEqual(launch.argv[start : start + len(flags)], flags)


class SharedConversationTest(Base):
    def test_headless_and_tui_share_their_container_home_and_transcript(self):
        client = ClaudeClient(CONFIG)
        self.assertEqual(client.headless()._home(), client.tui()._home())
        for env in ({}, {"CLAUDE_CONFIG_DIR": docker_home("w", "claude")}):
            with self.subTest(env=env):
                headless = self.launch(env=env).transcript
                self.assertEqual(headless, tui_launch(self.work, env=env).transcript)
                self.assertTrue(headless.startswith(env.get("CLAUDE_CONFIG_DIR", self.config_dir)))
