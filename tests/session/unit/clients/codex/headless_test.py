"""Codex headless launches, native events and usage limits."""

import dataclasses
import json
import os
import re
import sys
from datetime import UTC, datetime
from unittest import mock

from ensemble.session.clients.codex import CodexClient
from ensemble.session.clients.codex import common as codex_common
from ensemble.session.clients.codex.tui import decode
from ensemble.session.engine.types import (
    Activity,
    ClientBound,
    ClientContext,
    ConfigError,
    Container,
    LimitHit,
    Permission,
    Reply,
    Stderr,
    TurnEnd,
    TurnFailed,
    TurnStarted,
)
from ensemble.session.modes.headless import HeadlessMode
from tests.session.case import packaged_hosts
from tests.session.fakes.terminal import MemoryHost
from tests.session.fakes.worker import ScriptedWorker
from tests.session.unit.clients.codex.support import (
    AUTO,
    BYPASS,
    MODES,
    NATIVE,
    PLUGIN_OFF,
    TOOLS_OFF,
    Base,
    LaunchPolicyCases,
    committed_headless,
    packaged,
)
from tests.session.unit.clients.support import holds

CODEX = ("-s", "danger-full-access", "-c", 'approval_policy="on-request"')

HOOK = (sys.executable, "-I", "/protected/driver_hook.py", "context")


def default_config():
    with open(os.path.join(os.path.dirname(codex_common.__file__), "docker-config.toml")) as f:
        return f.read()


class HeadlessTest(Base):
    def test_native_turn_started_is_acceptance_and_resets_the_reply(self):
        run = self.client.headless()
        run.last_reply = "old"
        self.assertEqual(list(run.parse({"type": "turn.started"})), [TurnStarted()])
        self.assertEqual(list(run.parse({"type": "turn.completed"})), [TurnEnd("")])

    def test_failed_turn_preserves_native_error_kind(self):
        run = self.client.headless()
        self.assertEqual(
            list(
                run.parse(
                    {
                        "type": "turn.failed",
                        "error": {
                            "codex_error_info": "server_overloaded",
                            "message": "retry later",
                        },
                    }
                )
            ),
            [Stderr("retry later"), TurnFailed("server_overloaded", "retry later")],
        )

    def run_instance(self):
        run = self.client.headless()
        run.launch(dataclasses.replace(self.spec, prompt="hello"))
        run.commit()
        return run

    def test_native_binding_precedes_event(self):
        run = self.run_instance()
        (event,) = run.parse({"type": "thread.started", "thread_id": NATIVE})
        self.assertEqual(event, ClientBound(NATIVE))
        self.assertEqual(self.common.load(str(self.work), self.spec.sid).native_id, NATIVE)

    def test_bind_failure_never_emits_session_bound(self):
        run = self.run_instance()
        self.record().unlink()
        with self.assertRaises(ConfigError):
            list(run.parse({"type": "thread.started", "thread_id": NATIVE}))

    def test_complete_messages_and_final_result_keep_blank_lines(self):
        run = self.client.headless()
        texts = ["one\n\nsecond line\n", "[ensemble-progress:start] x", "three"]
        lines = [
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}})
            for text in texts
        ]
        lines.append(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10}}))
        self.assertEqual(
            list(run.events(lines)), [*(Reply(text) for text in texts), TurnEnd(texts[-1])]
        )

    def test_activity_and_invalid_record_diagnostics(self):
        run = self.client.headless()
        self.assertEqual(
            list(run.events(["not json", "[]"])),
            [
                Stderr("codex: invalid JSON event"),
                TurnFailed("invalid_output", "codex: invalid JSON event"),
            ]
            * 2,
        )
        self.assertEqual(
            list(run.parse({"type": "thread.started", "thread_id": "not-a-uuid"})),
            [Stderr("codex: invalid thread.started ID")],
        )
        self.assertEqual(
            list(run.parse({"type": "item.started", "item": {"type": "command_execution"}})),
            [Activity("item.started")],
        )

    def test_source_usage_and_quota_errors_without_reset_emit_limit(self):
        messages = (
            "You’ve hit your usage limit. Try again later.",
            (
                "You’ve hit your usage limit for GPT-5. Switch to another model now, or try again later."
            ),
            "Your workspace is out of credits. Add credits to continue.",
            (
                "You hit your spend cap set by the owner of your workspace. Ask an owner to increase your spend cap to continue."
            ),
            "Quota exceeded. Check your plan and billing details.",
        )
        for kind in ("error", "turn.failed"):
            for message in messages:
                with self.subTest(kind=kind, message=message):
                    run = self.client.headless()
                    record = (
                        {"type": kind, "message": message}
                        if kind == "error"
                        else {"type": kind, "error": {"message": message}}
                    )
                    self.assertEqual(list(run.parse(record)), [Stderr(message), LimitHit(None)])

    def test_paired_and_repeated_limit_errors_keep_stderr_once_per_run(self):
        message = "Quota exceeded. Check your plan and billing details."
        records = (
            {"type": "error", "message": message},
            {"type": "turn.failed", "error": {"message": message}},
            {"type": "error", "message": message},
        )
        run = self.run_instance()
        self.assertEqual(
            list(run.events(map(json.dumps, records))),
            [Stderr(message), LimitHit(), Stderr(message), Stderr(message)],
        )

    def test_non_limit_error_does_not_suppress_later_limit(self):
        run = self.run_instance()
        message = "Quota exceeded. Check your plan and billing details."
        self.assertEqual(
            list(run.parse({"type": "error", "message": "network error"})),
            [Stderr("network error")],
        )
        self.assertEqual(
            list(run.parse({"type": "turn.failed", "error": {"message": message}})),
            [Stderr(message), LimitHit()],
        )

    def test_turn_failed_is_a_failed_turn_and_an_error_is_not(self):
        run = self.run_instance()
        self.assertEqual(
            list(run.parse({"type": "error", "message": "retrying"})), [Stderr("retrying")]
        )
        self.assertEqual(
            list(run.parse({"type": "turn.failed", "error": {"message": "bad request"}})),
            [Stderr("bad request"), TurnFailed("turn_failed", "bad request")],
        )
        for record in (
            {"type": "turn.failed"},
            {"type": "turn.failed", "error": {"message": " "}},
            {"type": "turn.failed", "error": 5},
        ):
            with self.subTest(record=record):
                self.assertEqual(list(run.parse(record)), [TurnFailed("turn_failed", "")])

    def test_fresh_launch_resets_limit_guard(self):
        run = self.run_instance()
        message = "Quota exceeded. Check your plan and billing details."
        record = {"type": "error", "message": message}
        self.assertEqual(list(run.parse(record)), [Stderr(message), LimitHit()])
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        self.rollout()
        run.launch(dataclasses.replace(self.spec, prompt="again", resume=True))
        self.assertEqual(list(run.parse(record)), [Stderr(message), LimitHit()])

    def test_a_resume_spec_resumes_its_native_session_with_its_prompt(self):
        self.prepare()
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        self.rollout()
        spec = dataclasses.replace(self.spec, prompt="again", resume=True)
        argv = committed_headless(self.client, spec).argv
        start = argv.index("resume")
        self.assertEqual((argv[start + 1], argv[-2:]), (NATIVE, ("--", "again")))

    def test_source_local_reset_formats_become_unix_seconds(self):
        self.client.common.config["env"] = {"TZ": "UTC"}
        today = datetime.now(UTC)
        expected_today = today.replace(hour=16, minute=30, second=0, microsecond=0).timestamp()
        cases = (
            ("You’ve hit your usage limit. Try again at 4:30 PM.", expected_today),
            (
                (
                    "You’ve hit your usage limit. Upgrade to Pro, or try again at Oct 12th, 2026 4:30 PM."
                ),
                datetime(2026, 10, 12, 16, 30, tzinfo=UTC).timestamp(),
            ),
        )
        for message, expected in cases:
            with self.subTest(message=message):
                run = self.client.headless()
                self.assertEqual(
                    list(run.parse({"type": "error", "message": message})),
                    [Stderr(message), LimitHit(expected)],
                )

    def test_ambiguous_invalid_or_unrecognized_reset_remains_unknown(self):
        self.client.common.config["env"] = {"TZ": "America/New_York"}
        for value in (
            "Nov 1st, 2026 1:30 AM",
            "Mar 8th, 2026 2:30 AM",
            "Oct 32nd, 2026 4:30 PM",
            "16:30 UTC",
        ):
            message = f"You’ve hit your usage limit. Try again at {value}."
            with self.subTest(value=value):
                run = self.client.headless()
                self.assertEqual(
                    list(run.parse({"type": "error", "message": message})),
                    [Stderr(message), LimitHit(None)],
                )

    def test_arbitrary_error_is_not_usage_limit(self):
        run = self.client.headless()
        message = "network error mentioning a usage limit"
        self.assertEqual(list(run.parse({"type": "error", "message": message})), [Stderr(message)])


class ContainerLaunch(Base):
    def setUp(self):
        super().setUp()
        self.runtime = ClientContext(
            "host.docker.internal",
            Permission.AUTO,
            Container("/image/bin", "/image/python", "/image/source", "/run/ensemble"),
        )

    def test_both_modes_request_the_same_history_only_client_home(self):
        home = self.client.tui()._home()
        self.assertEqual(self.client.headless()._home(), home)
        self.assertEqual(
            (home.field, home.dir, home.env, home.credential, home.persistent),
            ("codex_dir", "codex", "CODEX_HOME", "codex-access-token", ("sessions",)),
        )

    def test_headless_mode_passes_the_image_runtime_and_home_to_its_host_worker(self):
        host = MemoryHost()
        host._inherit_env = False
        spec = dataclasses.replace(self.spec, name="w", client="codex", prompt="hello")
        with (
            mock.patch.object(host, "_client_context", return_value=self.runtime),
            mock.patch.object(
                host, "_client_env", return_value={"CODEX_HOME": str(self.codex_home)}
            ),
            mock.patch.object(
                host, "headless_worker", side_effect=lambda name, *_: ScriptedWorker(name, "f" * 32)
            ) as worker,
            mock.patch("shutil.which", return_value=None) as which,
            mock.patch.dict(os.environ, {"PERSONAL_CODEX_ENV": "private"}),
        ):
            mode = HeadlessMode.start(
                spec,
                self.client,
                registry=self.session_cls._config().registry,
                record=lambda _: None,
                host=host,
                generation="f" * 32,
            )
        name, launch, metadata = worker.call_args.args
        self.assertEqual((name, launch.argv[:2]), ("w", ("/image/bin/codex", "exec")))
        self.assertEqual(launch.env["CODEX_HOME"], str(self.codex_home))
        self.assertNotIn("PERSONAL_CODEX_ENV", launch.env)
        self.assertEqual(metadata["home_field"], "codex_dir")
        self.assertNotIn("codex", [call.args[0] for call in which.call_args_list])
        self.assertEqual(json.loads(metadata["home_persistent"]), ["sessions"])
        self.assertEqual(json.loads(metadata["home_files"]), {"config.toml": default_config()})
        self.assertTrue(all(done.wait(5) for done in mode.done))

    def test_unattended_flags_trust_the_real_cwd_whatever_its_name(self):
        link = self.root / "a.dotted link"
        link.symlink_to(self.cwd)
        real = json.dumps(os.path.realpath(self.cwd))
        self.assertEqual(
            codex_common.unattended_flags(str(link)),
            (
                "-c",
                'cli_auth_credentials_store="file"',
                "-c",
                'forced_login_method="chatgpt"',
                "-c",
                "check_for_update_on_startup=false",
                "-c",
                f'projects={{{real}={{trust_level="trusted"}}}}',
            ),
        )

    def test_a_container_launch_ends_its_flags_with_the_unattended_keys(self):
        supplied = ("-c", 'cli_auth_credentials_store="keyring"')
        spec = dataclasses.replace(self.spec, prompt="hi", flags=supplied)
        run = self.client.headless()
        run._context = self.runtime
        argv = list(run.launch(spec).argv)
        options = argv[: argv.index("--")]
        forced = list(codex_common.unattended_flags(str(self.cwd)))
        self.assertEqual(options[-len(forced) :], forced, argv)
        self.assertLess(options.index("-m"), len(options) - len(forced))
        self.assertTrue(holds(options[: -len(forced)], supplied), argv)
        local = committed_headless(self.client, dataclasses.replace(spec, sid="local"))
        self.assertNotIn('forced_login_method="chatgpt"', local.argv)

    def docker_config(self, text):
        path = os.path.join(
            os.environ["HOME"], ".ensemble", "docker-config", "codex", "config.toml"
        )
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_the_docker_config_is_the_codex_home_file_verbatim(self):
        text = '# mine\nservice_tier = "fast"\n'
        self.docker_config(text)
        self.assertEqual(self.client.headless()._home().files(), {"config.toml": text})

    def test_an_invalid_docker_config_fails_naming_it(self):
        path = self.docker_config("a = " + "[" * 5000 + "]" * 5000)
        with self.assertRaisesRegex(ConfigError, f"^Docker config {re.escape(path)}: invalid TOML"):
            self.client.headless()._home().files()
        path = self.docker_config("a = = 1")
        with self.assertRaisesRegex(
            ConfigError, f"^Docker config {re.escape(path)}: invalid TOML: "
        ):
            self.client.headless()._home().files()

    def test_headless_native_events_resume_in_a_terminal_and_back(self):
        run = self.client.headless()
        run._context = self.runtime
        first = run.launch(dataclasses.replace(self.spec, prompt="one"))
        run.commit()
        self.assertEqual(first.argv[:2], ("/image/bin/codex", "exec"))
        self.assertEqual(
            list(run.parse({"type": "thread.started", "thread_id": NATIVE})), [ClientBound(NATIVE)]
        )
        self.rollout()
        terminal = self.client.tui()
        terminal._context = self.runtime
        launch = terminal.launch(dataclasses.replace(self.spec, resume=True, prompt="two"), HOOK)
        plan = decode(json.loads(launch.argv[-1]))
        self.assertEqual(plan.selection, ("resume", NATIVE))
        self.assertEqual(plan.executable, "/image/bin/codex")
        resumed = self.client.headless()
        resumed._context = self.runtime
        command = resumed.launch(dataclasses.replace(self.spec, resume=True, prompt="three"))
        self.assertEqual(command.argv[command.argv.index("resume") + 1], NATIVE)
        self.assertEqual(command.env["CODEX_HOME"], first.env["CODEX_HOME"])
        events = list(
            resumed.events(
                map(
                    json.dumps,
                    [
                        {
                            "type": "item.completed",
                            "item": {"type": "agent_message", "text": "three"},
                        },
                        {"type": "turn.completed"},
                    ],
                )
            )
        )
        self.assertEqual(events, [Reply("three"), TurnEnd("three")])

    def test_container_resume_rejects_missing_mismatched_and_linked_history(self):
        self.prepare()
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        run = self.client.headless()
        run._context = self.runtime
        spec = dataclasses.replace(self.spec, resume=True)
        with self.assertRaisesRegex(ConfigError, "missing Codex session file"):
            run.launch(spec)
        path = self.rollout(cwd=str(self.extra))
        with self.assertRaisesRegex(ConfigError, "mismatched Codex session"):
            run.launch(spec)
        path = self.rollout()
        outside = self.extra / path.name
        path.rename(outside)
        path.symlink_to(outside)
        with self.assertRaises(ConfigError):
            run.launch(spec)


class LaunchPolicy(LaunchPolicyCases, Base):
    mode = "headless"

    def argv(self, client, spec, *, resume=False):
        return list(
            committed_headless(client, dataclasses.replace(spec, prompt="hi", resume=resume)).argv
        )


class PolicyReplay(Base):
    def test_resumed_snapshot_gets_actual_client_context_without_replacing_saved_policy(self):
        for old_client in (None, "stale"):
            with self.subTest(old_client=old_client):
                saved_env = {"K": "saved"}
                if old_client is not None:
                    saved_env["ENSEMBLE_CLIENT"] = old_client
                spec = dataclasses.replace(
                    self.spec, client="codex", sid=f"context-{old_client}", env=saved_env
                )
                committed_headless(self.client, dataclasses.replace(spec, prompt="one"))
                self.common.bind(str(self.work), spec.sid, NATIVE)
                self.rollout()
                resumed = dataclasses.replace(
                    spec,
                    prompt="two",
                    resume=True,
                    env={"K": "changed", "ENSEMBLE_CLIENT": "wrong"},
                )
                host = MemoryHost()
                with mock.patch.object(
                    host,
                    "headless_worker",
                    side_effect=lambda name, *_: ScriptedWorker(name, "f" * 32),
                ) as worker:
                    HeadlessMode.start(
                        resumed,
                        self.client,
                        registry=self.session_cls._config().registry,
                        record=lambda _info: None,
                        host=host,
                        generation="f" * 32,
                    )
                env = worker.call_args.args[1].env
                self.assertEqual(env["ENSEMBLE_CLIENT"], "codex")
                self.assertEqual(env["K"], "saved")
                self.assertEqual(
                    dict(self.common.load(str(self.work), spec.sid).snapshot.env), saved_env
                )

    def test_snapshot_replays_supplied_flags_and_environment(self):
        flags = (
            "-s",
            "workspace-write",
            "-c",
            'approval_policy="on-request"',
            "--add-dir",
            str(self.extra),
        )
        spec = dataclasses.replace(self.spec, flags=flags, env={"K": "saved"})
        first = committed_headless(self.client, dataclasses.replace(spec, prompt="one"))
        state = self.common.bind(str(self.work), spec.sid, NATIVE)
        self.rollout()
        changed = dataclasses.replace(
            spec, flags=("-s", "danger-full-access"), env={"K": "changed"}
        )
        client = CodexClient({**self.client.config, "env": {"K": "config"}})
        resumed = committed_headless(
            client, dataclasses.replace(changed, prompt="two", resume=True)
        )
        self.assertEqual(state.snapshot.flags, tuple(flags))
        self.assertEqual(first.env["K"], "saved")
        self.assertEqual(resumed.env["K"], "saved")
        self.assertIn("workspace-write", resumed.argv)
        self.assertNotIn("danger-full-access", resumed.argv)


class ConfiguredFlags(Base):
    OWN = ("-c", "plugins.x@m.enabled=false")
    NEW = ("-c", "plugins.y@m.enabled=false")

    def test_the_configs_flags_precede_the_specs_and_a_resume_reads_the_current_ones(self):
        client = CodexClient({**self.client.config, "flags": list(self.OWN)})
        spec = dataclasses.replace(self.spec, flags=CODEX)
        first = committed_headless(client, dataclasses.replace(spec, prompt="one"))
        start = first.argv.index(self.OWN[0])
        self.assertEqual(first.argv[start : start + 6], (*self.OWN, *CODEX))
        state = self.common.bind(str(self.work), spec.sid, NATIVE)
        self.assertEqual(state.snapshot.flags, CODEX)
        self.rollout()
        changed = CodexClient({**self.client.config, "flags": list(self.NEW)})
        resumed = committed_headless(
            changed, dataclasses.replace(spec, flags=(), prompt="two", resume=True)
        )
        start = resumed.argv.index(self.NEW[0])
        self.assertEqual(resumed.argv[start : start + 6], (*self.NEW, *CODEX))
        self.assertNotIn(self.OWN[1], resumed.argv)

    def test_a_session_started_before_the_tools_went_off_resumes_without_them(self):
        committed_headless(self.client, dataclasses.replace(self.spec, flags=CODEX, prompt="one"))
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        self.rollout()
        spec = dataclasses.replace(self.spec, flags=CODEX, prompt="two", resume=True)
        argv = committed_headless(packaged(), spec).argv
        for pair in TOOLS_OFF:
            with self.subTest(pair=pair):
                self.assertTrue(holds(argv, pair), argv)

    def test_the_packaged_config_turns_the_ensemble_plugin_off(self):
        spec = dataclasses.replace(self.spec, flags=CODEX, prompt="hi")
        argv = committed_headless(packaged(), spec).argv
        start = argv.index(PLUGIN_OFF[1]) - 1
        self.assertEqual(argv[start : start + 2], PLUGIN_OFF)
        self.assertEqual(argv[1:start], ("exec", "--json", "--skip-git-repo-check", *AUTO))
        self.assertEqual(argv.count(PLUGIN_OFF[1]), 1)

    def test_the_packaged_config_turns_the_browser_computer_use_apps_and_suggestions_off(self):
        spec = dataclasses.replace(self.spec, flags=CODEX, prompt="hi")
        argv = committed_headless(packaged(), spec).argv
        for pair in TOOLS_OFF:
            with self.subTest(pair=pair):
                self.assertTrue(holds(argv, pair), argv)
                self.assertEqual(argv.count(pair[1]), 1)
        self.assertNotIn("plugins", [argv[i + 1] for i, w in enumerate(argv) if w == "--disable"])

    def test_a_local_flags_list_without_an_entry_gives_that_tool_back(self):
        local = self.root / "config.local.toml"
        kept = [word for pair in (PLUGIN_OFF, *TOOLS_OFF[:-2], TOOLS_OFF[-1]) for word in pair]
        local.write_text(f"[session.clients.codex]\nflags = {json.dumps(kept)}\n", encoding="utf-8")
        spec = dataclasses.replace(self.spec, flags=CODEX, prompt="hi")
        argv = committed_headless(packaged(str(local)), spec).argv
        self.assertNotIn("apps", argv)
        self.assertTrue(holds(argv, TOOLS_OFF[-1]), argv)

    def test_only_the_runs_permission_modes_flags_reach_codex_never_its_record(self):
        client = CodexClient({**self.client.config, "modes": MODES})
        for permission, present, absent in (
            (Permission.AUTO, AUTO, BYPASS),
            (Permission.YOLO, (BYPASS,), "-s"),
        ):
            with self.subTest(permission=permission):
                spec = dataclasses.replace(self.spec, sid=f"run-{permission}", prompt="hi")
                argv = committed_headless(client, spec, permission).argv
                self.assertTrue(holds(argv, present), argv)
                self.assertNotIn(absent, argv)
                record = client.common.load(str(self.work), spec.sid)
                self.assertEqual(record.snapshot.flags, ())

    def test_a_resume_runs_in_the_mode_of_the_host_it_runs_on(self):
        client = CodexClient({**self.client.config, "modes": MODES})
        committed_headless(client, dataclasses.replace(self.spec, prompt="one"))
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        self.rollout()
        spec = dataclasses.replace(self.spec, prompt="two", resume=True)
        argv = committed_headless(client, spec, Permission.YOLO).argv
        self.assertIn(BYPASS, argv)
        self.assertNotIn("-s", argv)

    def test_the_packaged_config_gives_docker_codex_only_yolo_flags_every_other_host_only_auto(
        self,
    ):
        hosts = packaged_hosts()
        self.assertEqual(hosts, {"tmux": Permission.AUTO, "docker": Permission.YOLO})
        words = {Permission.AUTO: ("-s", *AUTO[1::2]), Permission.YOLO: (BYPASS,)}
        for name, permission in hosts.items():
            with self.subTest(host=name):
                spec = dataclasses.replace(self.spec, sid=f"run-{name}", prompt="hi")
                argv = committed_headless(packaged(), spec, permission).argv
                other = Permission.YOLO if permission == Permission.AUTO else Permission.AUTO
                self.assertTrue(all(argv.count(w) == 1 for w in words[permission]), argv)
                self.assertFalse([w for w in words[other] if w in argv], argv)

    def test_bad_modes_are_refused(self):
        for modes, message in (
            (
                {"turbo": []},
                "[clients.codex].modes must be a table from auto and yolo to a list of strings",
            ),
            (
                {"yolo": "x"},
                "[clients.codex].modes must be a table from auto and yolo to a list of strings",
            ),
            ({"yolo": ["-C", "/x"]}, "-C /x: the Codex client sets hooks, session and cwd"),
            (
                {"auto": ["--add-dir", "/x"]},
                "--add-dir: a permission mode's flags grant no directory",
            ),
        ):
            with self.subTest(modes=modes), self.assertRaises(ConfigError) as caught:
                CodexClient({**self.client.config, "modes": modes})
            self.assertEqual(str(caught.exception), message)

    def test_bad_configured_flags_are_refused(self):
        for flags, message in (
            ("-c", "[clients.codex].flags must be a list of strings"),
            ([1], "[clients.codex].flags must be a list of strings"),
            (["-C", "/x"], "-C /x: the Codex client sets hooks, session and cwd"),
            (["-c", "hooks.x=1"], "-c hooks.x=1: the Codex client sets hooks, session and cwd"),
            (["--add-dir", "/x"], "--add-dir: [clients.codex] flags grant no directory"),
            (["-c", "x=1", "--", "extra"], "--: [clients.codex] flags pass no literal argument"),
        ):
            with self.subTest(flags=flags), self.assertRaises(ConfigError) as caught:
                CodexClient({**self.client.config, "flags": flags})
            self.assertEqual(str(caught.exception), message)
