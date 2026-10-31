"""OpenCode's TUI: launch argv and env, flag refusals, hook events and the hook callback."""

import json
import os
from dataclasses import replace
from pathlib import Path
from unittest import mock

from ensemble.session.clients.opencode import OpenCodeClient, tui
from ensemble.session.clients.opencode.common import Bindings
from ensemble.session.engine.types import (
    LOCAL_CONTEXT,
    Access,
    Activity,
    Blocked,
    ClientBound,
    ClientContext,
    ClientHome,
    ConfigError,
    HookCall,
    HookKind,
    Launch,
    Permission,
    Reply,
    Spec,
    TurnEnd,
    TurnFailed,
    TurnStarted,
)
from tests.session.case import Case
from tests.session.unit.clients.opencode.support import (
    DOCKER_CONTEXT,
    IMAGE_CLIENT,
    SES,
    fake_run,
    packaged_table,
    titled_session,
)

HOOK = ("/bin/ensemble-session", "hook", "/home/x/.ensemble/tui/w1")
MODEL = ("--model", "ollama/qwen3.6:35b-mlx")
OTHER = "ses_otherOtherOtherOther0001"


class Base(Case):
    def setUp(self):
        self.cwd = os.path.realpath(os.path.join(self.home, "project"))
        os.mkdir(self.cwd)
        self.client = OpenCodeClient(packaged_table())
        self.enterContext(mock.patch("subprocess.run", side_effect=fake_run))

    def spec(self, **fields) -> Spec:
        fields.setdefault("prompt", "Hi there")
        fields.setdefault("access", Access(project=True, cwd=self.cwd))
        return Spec(workdir=self.home, sid="run-1", **fields)

    def run_tui(self, client=None, context: ClientContext = LOCAL_CONTEXT) -> tui.OpenCodeTui:
        run = (client or self.client).tui()
        assert isinstance(run, tui.OpenCodeTui)
        run._context = context
        return run

    def launch(self, spec: Spec, client=None, context=LOCAL_CONTEXT) -> Launch:
        return self.run_tui(client, context).launch(spec, HOOK)


class LaunchTest(Base):
    def test_a_fresh_start_submits_its_prompt(self):
        launch = self.launch(self.spec(tier=1))
        self.assertEqual(
            launch.argv,
            ("opencode", "--model", "ollama/qwen3.6:35b-mlx", "--prompt=Hi there"),
        )
        self.assertEqual((launch.cwd, launch.project), (self.cwd, True))

    def test_no_tier_runs_the_configured_model(self):
        launch = self.launch(self.spec(prompt="", effort="high"))
        self.assertEqual(launch.argv, ("opencode", *MODEL))
        tiers = {"1": "ollama/a", "2": "ollama/a", "3": "ollama/a", "4": "ollama/a"}
        client = OpenCodeClient({**packaged_table(), "tiers": tiers, "model": "ollama/b"})
        self.assertEqual(
            self.launch(self.spec(prompt=""), client).argv[1:], ("--model", "ollama/b")
        )
        self.assertEqual(
            self.launch(self.spec(prompt="", tier=2), client).argv[1:], ("--model", "ollama/a")
        )

    def test_a_resume_selects_its_session_and_opens_with_the_prompt(self):
        self.client.common.bind(self.cwd, "run-1", SES)
        run = self.run_tui()
        launch = run.launch(self.spec(resume=True, prompt="Fix it."), HOOK)
        self.assertEqual(launch.argv, ("opencode", "--session", SES, *MODEL))
        self.assertEqual(run.opening, (tui.START, "Fix it."))
        self.assertTrue(run._opening_prompt)  # it is the prompt: not deferred past a launch turn
        self.assertEqual(launch.env["ENSEMBLE_OPENCODE_SESSION"], SES)
        bare = self.run_tui()
        bare.launch(self.spec(resume=True, prompt=""), HOOK)
        self.assertIsNone(bare.opening)

    def test_a_resume_of_an_unbound_headless_run_takes_its_titled_session(self):
        data = os.path.join(self.home, "data")
        titled_session(os.path.join(data, "opencode", "opencode.db"), SES, "ensemble-run-1", 1)
        launch = self.run_tui().launch(self.spec(resume=True, env={"XDG_DATA_HOME": data}), HOOK)
        self.assertEqual(launch.argv[:3], ("opencode", "--session", SES))
        self.assertEqual(launch.env["ENSEMBLE_OPENCODE_SESSION"], SES)

    def test_a_resume_without_a_binding_starts_a_fresh_session_with_its_prompt(self):
        """A run killed before its session bound resumes as a fresh one."""
        run = self.run_tui()
        launch = run.launch(self.spec(resume=True, prompt="Fix it."), HOOK)
        self.assertEqual(launch.argv, ("opencode", *MODEL, "--prompt=Fix it."))
        self.assertIsNone(run.opening)
        self.assertEqual(launch.env["ENSEMBLE_OPENCODE_SESSION"], "")
        self.assertTrue(run.fresh)

    def test_the_env_carries_the_plugin_and_the_hook(self):
        launch = self.launch(self.spec())
        config = json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["plugin"], [Path(tui.PLUGIN).resolve().as_uri()])
        self.assertTrue(os.path.isfile(tui.PLUGIN))
        self.assertEqual(config["permission"]["bash"], "ask")
        self.assertEqual(json.loads(launch.env["ENSEMBLE_OPENCODE_HOOK"]), list(HOOK))
        self.assertEqual(launch.env["ENSEMBLE_OPENCODE_SESSION"], "")
        self.assertEqual(launch.env["OPENCODE_DISABLE_AUTOUPDATE"], "1")
        self.assertIn("OPENCODE_PURE", launch.unset)

    def test_flags_follow_the_config_and_mode(self):
        client = OpenCodeClient({**packaged_table(), "flags": ["--agent", "build"]})
        context = replace(LOCAL_CONTEXT, permission=Permission.YOLO)
        launch = self.launch(self.spec(flags=("--log-level", "INFO")), client, context)
        self.assertEqual(
            launch.argv,
            (
                "opencode",
                *MODEL,
                "--agent",
                "build",
                "--auto",
                "--log-level",
                "INFO",
                "--prompt=Hi there",
            ),
        )

    def test_the_executable_is_argv0(self):
        client = OpenCodeClient({**packaged_table(), "executable": "/opt/oc1/bin/opencode"})
        launch = self.launch(self.spec(prompt=""), client)
        self.assertEqual(launch.argv, ("/opt/oc1/bin/opencode", *MODEL))

    def test_a_2x_opencode_is_refused(self):
        with (
            mock.patch("tests.session.unit.clients.opencode.support.VERSION", "2.0.32"),
            self.assertRaisesRegex(ConfigError, r"opencode 2\.0\.32 \(opencode\) is not 1\.x"),
        ):
            self.launch(self.spec())

    def test_a_container_launch_runs_the_images_opencode_and_plugin(self):
        client = OpenCodeClient({**packaged_table(), "executable": "/Users/x/oc1/bin/opencode"})
        data = "/Users/x/.ensemble/docker-state/w/opencode"
        with mock.patch("subprocess.run", side_effect=fake_run) as run:
            launch = self.launch(
                self.spec(env={"XDG_DATA_HOME": data}), client, context=DOCKER_CONTEXT
            )
        self.assertEqual(
            launch.argv, ("/opt/ensemble/bin/opencode", *MODEL, "--auto", "--prompt=Hi there")
        )
        self.assertNotIn(["--version"], [call.args[0][1:] for call in run.call_args_list])
        config = json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["plugin"], [f"file://{IMAGE_CLIENT}/plugin.js"])
        self.assertEqual(
            config["provider"]["ollama"]["options"]["baseURL"],
            "http://host.docker.internal:11434/v1",
        )
        self.assertEqual(launch.transcript, f"{data}/opencode/opencode.db")
        self.assertEqual(self.launch(self.spec()).transcript, "")

    def test_yolo_approves_tools_before_the_tui_can_auto_reply(self):
        access = Access(cwd=self.cwd, commands=("git status",))
        for context in (DOCKER_CONTEXT, replace(LOCAL_CONTEXT, permission=Permission.YOLO)):
            with self.subTest(container=context.container is not None):
                launch = self.launch(self.spec(access=access), context=context)
                permission = json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])["permission"]
                for tool in ("bash", "read", "external_directory", "doom_loop"):
                    self.assertEqual(permission[tool], "allow", tool)
                self.assertNotIn("*", permission)
                self.assertEqual(permission["edit"][".git/*"], "deny")
                self.assertEqual(permission["edit"][".opencode/*"], "deny")

    def test_both_modes_name_the_docker_home(self):
        home = ClientHome("opencode_dir", "opencode", "XDG_DATA_HOME", "none")
        self.assertEqual(self.run_tui()._home(), home)
        headless = self.client.headless()
        assert headless is not None
        self.assertEqual(headless._home(), home)
        for adapter in (tui.OpenCodeTui, type(headless)):
            with self.subTest(adapter=adapter.__name__):
                self.assertNotIn("_home", vars(adapter))


class RefusalTest(Base):
    SELECTORS = (
        ("-s", SES),
        ("--session", SES),
        (f"--session={SES}",),
        ("-c",),
        ("--continue",),
        ("--fork",),
    )

    def test_selectors_in_flags_are_refused(self):
        for flags in self.SELECTORS:
            with self.subTest(source="spec", flags=flags), self.assertRaises(ConfigError):
                self.launch(self.spec(flags=flags))
            with self.subTest(source="config", flags=flags), self.assertRaises(ConfigError):
                OpenCodeClient({**packaged_table(), "flags": list(flags)})
            with self.subTest(source="mode", flags=flags), self.assertRaises(ConfigError):
                OpenCodeClient({**packaged_table(), "modes": {"auto": list(flags)}})

    def test_prompt_pure_and_pure_env_are_refused(self):
        for flags in (("--prompt", "x"), ("--prompt=x",), ("--pure",)):
            with self.subTest(source="spec", flags=flags), self.assertRaises(ConfigError):
                self.launch(self.spec(flags=flags))
            client = OpenCodeClient({**packaged_table(), "flags": list(flags)})
            with self.subTest(source="config", flags=flags), self.assertRaises(ConfigError):
                self.launch(self.spec(), client)
            client = OpenCodeClient({**packaged_table(), "modes": {"auto": list(flags)}})
            with self.subTest(source="mode", flags=flags), self.assertRaises(ConfigError):
                self.launch(self.spec(), client)
        for spec in (
            self.spec(env={"OPENCODE_PURE": "1"}),
            self.spec(access=Access(project=True, cwd=self.cwd, env={"OPENCODE_PURE": "1"})),
        ):
            with self.subTest(env=spec.env), self.assertRaisesRegex(ConfigError, "OPENCODE_PURE"):
                self.launch(spec)
        client = OpenCodeClient({**packaged_table(), "env": {"OPENCODE_PURE": "1"}})
        with self.assertRaisesRegex(ConfigError, "OPENCODE_PURE"):
            self.launch(self.spec(), client)

    def test_headless_keeps_pure(self):
        client = OpenCodeClient({**packaged_table(), "flags": ["--pure"]})
        run = client.headless()
        assert run is not None
        self.assertIn("--pure", run.launch(self.spec()).argv)


class CommitTest(Base):
    def test_a_fresh_start_unbinds_and_a_resume_keeps_its_binding(self):
        self.client.common.bind(self.cwd, "run-1", SES)
        run = self.run_tui()
        run.launch(self.spec(resume=True), HOOK)
        run.commit()
        self.assertEqual(self.client.common.native(self.cwd, "run-1"), SES)
        run = self.run_tui()
        run.launch(self.spec(), HOOK)
        run.commit()
        self.assertIsNone(self.client.common.native(self.cwd, "run-1"))

    def test_the_callback_names_the_cwd_and_store(self):
        run = self.run_tui()
        run.launch(self.spec(), HOOK)
        callback = run.hook_callback()
        self.assertEqual(callback.module, tui.__name__)
        self.assertEqual(callback.state, {"cwd": self.cwd, "store": self.client.common.store})

    def test_restore_checks_its_state(self):
        run = self.run_tui()
        run.restore(self.spec(), {"cwd": self.cwd, "store": self.client.common.store})
        for state in ({}, {"cwd": "relative", "store": "/s"}, {"cwd": "/c", "store": 3}):
            with self.subTest(state=state), self.assertRaisesRegex(ConfigError, "bad state"):
                self.run_tui().restore(self.spec(), state)


class OnHookTest(Base):
    def call(self, payload, **state) -> dict | None:
        state = {"cwd": self.cwd, "store": self.client.common.store, **state}
        return tui.on_hook(HookCall("session.idle", payload, "run-1", self.home, None, state))

    def bound(self) -> str | None:
        return Bindings(self.client.common.store).native(self.cwd, "run-1")

    def test_an_unbound_run_binds_the_first_session(self):
        payload = {"session_id": SES, "last_assistant_message": "hi"}
        self.assertEqual(self.call(payload), payload)
        self.assertEqual(self.bound(), SES)

    def test_its_own_session_passes(self):
        self.client.common.bind(self.cwd, "run-1", SES)
        self.assertEqual(self.call({"session_id": SES}), {"session_id": SES})

    def test_another_session_is_dropped(self):
        self.client.common.bind(self.cwd, "run-1", SES)
        self.assertIsNone(self.call({"session_id": OTHER}))
        self.assertEqual(self.bound(), SES)

    def test_a_bad_id_or_state_is_dropped(self):
        for payload in ({}, {"session_id": "--auto"}, {"session_id": 3}):
            with self.subTest(payload=payload):
                self.assertIsNone(self.call(payload))
        self.assertIsNone(self.call({"session_id": SES}, cwd="relative"))
        self.assertIsNone(self.call({"session_id": SES}, store=None))
        self.assertIsNone(self.bound())

    def test_a_corrupt_binding_is_rebound(self):
        self.client.common.bind(self.cwd, "run-1", OTHER)
        (name,) = os.listdir(self.client.common.store)
        Path(self.client.common.store, name).write_text("not json")
        self.assertEqual(self.call({"session_id": SES}), {"session_id": SES})
        self.assertEqual(self.bound(), SES)


class EventsTest(Base):
    def test_user_message_starts_once_and_late_terminal_does_not_finish_new_turn(self):
        run = self.run_tui()
        run.hook_events(tui.START, {"session_id": SES})
        for turn in ("msg-first", "msg-next"):
            payload = {"session_id": SES, "turn_id": turn}
            self.assertEqual(run.hook_events("turn.started", payload), [TurnStarted()])
            self.assertEqual(run.hook_events("turn.started", payload), [])
            self.assertEqual(run.hook_events("session.busy", payload), [Activity()])
            self.assertEqual(
                run.hook_events("session.error", {**payload, "error": "APIError"}),
                [TurnFailed("APIError")],
            )
            self.assertEqual(run.hook_events("session.idle", payload), [])
        self.assertEqual(
            run.hook_events("turn.started", {"session_id": SES, "turn_id": "msg-third"}),
            [TurnStarted()],
        )
        self.assertEqual(
            run.hook_events("session.idle", {"session_id": SES, "turn_id": "msg-first"}), []
        )
        self.assertEqual(
            run.hook_events(
                "message.completed",
                {"session_id": SES, "turn_id": "msg-first", "text": "late old reply"},
            ),
            [],
        )
        self.assertEqual(
            run.hook_events("turn.started", {"session_id": "ses_foreign", "turn_id": "other"}), []
        )

    def test_completed_replies_are_delivered_and_replayed_before_turn_end(self):
        records = [
            ("message.completed", {"session_id": SES, "text": text})
            for text in ("[ensemble-progress:start] working", "intermediate", "done")
        ]
        records.append(("session.idle", {"session_id": SES, "last_assistant_message": "done"}))
        expected = [
            ClientBound(SES),
            Reply("[ensemble-progress:start] working"),
            Reply("intermediate"),
            Reply("done"),
            TurnEnd("done"),
        ]
        self.assertEqual(self.run_tui().replay(records), expected)

    def test_empty_or_invalid_completed_text_is_ignored(self):
        run = self.run_tui()
        run.hook_events(tui.START, {"session_id": SES})
        for value in (None, 3, "", " \n "):
            with self.subTest(value=value):
                self.assertEqual(
                    run.hook_events("message.completed", {"session_id": SES, "text": value}), []
                )

    def test_the_hook_labels(self):
        self.assertEqual(
            dict(tui.OpenCodeTui.hooks),
            {
                "session.idle": HookKind.TURN_END,
                "turn.started": HookKind.TURN_STARTED,
                "session.error": HookKind.TURN_FAILED,
                "permission.asked": HookKind.BLOCKED,
                "question.asked": HookKind.BLOCKED,
                "session.busy": HookKind.ACTIVITY,
                "permission.replied": HookKind.ACTIVITY,
                "question.replied": HookKind.ACTIVITY,
                "question.rejected": HookKind.ACTIVITY,
                "tool": HookKind.ACTIVITY,
            },
        )

    def test_permission_asks_follow_the_selected_modes_auto_flag(self):
        cases = [
            (packaged_table(), permission, permission is Permission.YOLO)
            for permission in Permission
        ]
        cases.extend(
            (
                {**packaged_table(), "modes": {"auto": ["--auto"], "yolo": []}},
                permission,
                permission is Permission.AUTO,
            )
            for permission in (Permission.AUTO, Permission.YOLO)
        )
        for table, permission, auto in cases:
            for restored in (False, True):
                with self.subTest(permission=permission, auto=auto, restored=restored):
                    run = self.run_tui(
                        OpenCodeClient(table), replace(LOCAL_CONTEXT, permission=permission)
                    )
                    if restored:
                        run.restore(self.spec(), {"cwd": self.cwd, "store": run.common.store})
                    else:
                        run.launch(self.spec(), HOOK)
                    self.assertEqual("permission.asked" in run.hooks, not auto)
                    self.assertEqual(run.hooks["question.asked"], HookKind.BLOCKED)
                    records = [
                        ("permission.asked", {"session_id": SES, "permission": "custom_tool"}),
                        ("permission.replied", {"session_id": SES, "reply": "once"}),
                        ("question.asked", {"session_id": SES, "questions": ["Database"]}),
                        ("question.replied", {"session_id": SES}),
                        ("session.idle", {"session_id": SES, "last_assistant_message": "Done"}),
                        ("session.error", {"session_id": SES, "error": "APIError"}),
                    ]
                    expected = [ClientBound(SES)]
                    if not auto:
                        expected.append(Blocked("custom_tool"))
                    expected.extend(
                        [
                            Activity(""),
                            Blocked("question: Database"),
                            Activity(""),
                            TurnEnd("Done"),
                            TurnFailed("APIError", ""),
                        ]
                    )
                    events = (
                        run.replay(records)
                        if restored
                        else [
                            event
                            for label, payload in records
                            for event in run.hook_events(label, payload)
                        ]
                    )
                    self.assertEqual(events, expected)

    def test_events(self):
        run = self.run_tui()
        self.assertEqual(run.hook_events(tui.START, {"session_id": SES}), [ClientBound(SES)])
        self.assertEqual(run.hook_events("session.busy", {"session_id": SES}), [Activity("")])
        self.assertEqual(
            run.hook_events("tool", {"session_id": SES, "tool_name": "bash"}), [Activity("bash")]
        )
        self.assertEqual(
            run.hook_events("permission.asked", {"session_id": SES, "permission": "bash"}),
            [Blocked("bash")],
        )
        self.assertEqual(
            run.hook_events("question.asked", {"session_id": SES, "questions": ["Database"]}),
            [Blocked("question: Database")],
        )
        self.assertEqual(
            run.hook_events("question.asked", {"session_id": SES, "questions": ["A", "", 3, "B"]}),
            [Blocked("question: A; B")],
        )
        self.assertEqual(
            run.hook_events("question.asked", {"session_id": SES, "questions": "A"}),
            [Blocked("question")],
        )
        for label in ("question.replied", "question.rejected"):
            self.assertEqual(run.hook_events(label, {"session_id": SES}), [Activity("")])
        self.assertEqual(
            run.hook_events("session.idle", {"session_id": SES, "last_assistant_message": "Done."}),
            [TurnEnd("Done.")],
        )
        self.assertEqual(run.hook_events("session.idle", {"session_id": SES}), [TurnEnd("")])
        self.assertEqual(
            run.hook_events(
                "session.error",
                {"session_id": SES, "error": "APIError", "message": "connection refused"},
            ),
            [TurnFailed("APIError", "connection refused")],
        )
        self.assertEqual(
            run.hook_events("session.error", {"session_id": SES}), [TurnFailed("unknown", "")]
        )

    def test_the_client_is_opencode_tui(self):
        self.assertIsInstance(self.client.tui(), tui.OpenCodeTui)
