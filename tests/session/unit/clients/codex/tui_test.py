"""Codex interactive launch policy, hook events and native limits."""

import contextlib
import dataclasses
import hashlib
import io
import json
import os
import shlex
import sys
import sysconfig
import time
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from ensemble.session.clients.codex import CodexClient, common, tui
from ensemble.session.clients.codex.tui import CodexTui
from ensemble.session.engine.types import (
    Access,
    Activity,
    Blocked,
    ClientBound,
    ClientContext,
    ConfigError,
    Container,
    HookCall,
    HookCallback,
    HookKind,
    Key,
    LimitHit,
    Paste,
    Permission,
    Reply,
    TurnEnd,
    TurnFailed,
    TurnStarted,
)
from ensemble.session.modes.tui import TuiMode
from ensemble.utils import LOCAL_ENV_VARS
from tests.session.case import ROOTS, SRC
from tests.session.codex_tui import PlanCase
from tests.session.fakes.terminal import MemoryHost
from tests.session.source_roots import PACKAGE_ROOTS
from tests.session.unit.clients.codex.support import (
    AUTO,
    BYPASS,
    CHILD,
    MODES,
    NATIVE,
    PLUGIN_OFF,
    TOOLS_OFF,
    UNIFIED,
    Base,
    committed_tui,
    holds,
    packaged,
)

HOOK = (
    sys.executable,
    "-I",
    "/protected/driver_hook.py",
    "context",
    json.dumps([SRC["session"], SRC["utils"]]),
)

# A hook log of two turns, as (label, Stop text) pairs.
TURNS = (
    ("SessionStart", ""),
    ("UserPromptSubmit", ""),
    ("Stop", "a"),
    ("UserPromptSubmit", ""),
    ("Stop", "final"),
)
TOOL = {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command"}}


def _said(role, *texts):
    """A Codex session file message record."""
    kind = "output_text" if role == "assistant" else "input_text"
    content = [{"type": kind, "text": text} for text in texts]
    return {
        "type": "response_item",
        "payload": {"type": "message", "role": role, "content": content},
    }


def _failed(message, info="other"):
    """A Codex session file's record of a turn that failed."""
    error = {"message": message, "codex_error_info": info}
    return {"type": "event_msg", "payload": {"type": "task_complete", "error": error}}


def _append(path, *records):
    with path.open("a", encoding="utf-8") as stream:
        stream.writelines(json.dumps(record) + "\n" for record in records)


# Codex 0.162.1's own renders of its dialogs (openai/codex tui insta snapshots).
DIALOG_SCREENS = {
    "Trust this folder?": (
        "  Folder access\n  /workspace/project\n\n"
        "  Trust this folder? Codex can read, edit, and run files here,\n"
        "  subject to your permission settings.\n\n"
        "› 1. Trust and continue\n  2. Quit\n\n  enter continue · esc quit"
    ),
    "Config, hooks, and exec policies from untrusted folders stay disabled.": (
        "  Folder access\n  /workspace/project\n\n"
        "  Config, hooks, and exec policies from untrusted folders stay\n"
        "  disabled. Trusted project folders can still contribute settings.\n\n"
        "› 1. Open restricted\n  2. Back to Agent Command Center\n\n  enter continue · esc back"
    ),
    "Hooks need review": (
        "\n  Hooks need review\n  2 hooks are new or changed.\n\n"
        "› 1. Review hooks\n  2. Trust all and continue\n\n  enter confirm · esc skip"
    ),
    "Working directory · resume": (
        "\n  Working directory · resume\n"
        "  Session = latest cwd recorded in the resumed session\n\n"
        "› 1. Use session directory (/work/session)\n  2. Use current directory (/work/current)"
    ),
    "This conversation is open in another app": (
        "  🔒   This conversation is open in another app  r to retry\n"
        "      Close it there and press R to continue here.\n\n"
        "   r retry   f fork   ←/esc command center   ⌃c/q exit"
    ),
    "Sign in with ChatGPT to use Codex": (
        "  Sign in with ChatGPT to use Codex as part of your paid plan\n"
        "  or connect an API key for usage-based billing\n\n"
        "> 1. Sign in with ChatGPT\n"
        "     Usage included with Plus, Pro, Business, and Enterprise plans\n\n"
        "  Press Enter to continue"
    ),
}


class TuiTest(Base):
    def test_container_runtime_projects_only_this_identity_without_host_executable(self):
        host = MemoryHost()
        runtime = ClientContext(
            "host.docker.internal",
            Permission.AUTO,
            Container(
                "/opt/ensemble/bin",
                "/opt/ensemble/venv/bin/python",
                "/opt/ensemble/src",
                "/run/ensemble",
            ),
        )
        spec = dataclasses.replace(self.spec, name="w", client="codex", prompt="hello")
        with (
            mock.patch.object(host, "_client_context", return_value=runtime),
            mock.patch.object(
                host, "_client_env", return_value={"CODEX_HOME": str(self.codex_home)}
            ),
            mock.patch(
                "shutil.which",
                side_effect=lambda name, **_: (
                    f"/hooks/{name}" if name == "ensemble-session" else None
                ),
            ) as which,
        ):
            TuiMode.start(
                spec,
                self.client,
                registry=self.session_cls._config().registry,
                record=lambda _: None,
                host=host,
            )
        _, launch = host.calls.terminal.call_args.args
        metadata = host.calls.terminal.call_args.kwargs["metadata"]
        plan = tui.decode(json.loads(launch.argv[-1]))
        self.assertEqual(launch.argv[0], "/opt/ensemble/venv/bin/python")
        self.assertEqual(plan.executable, "/opt/ensemble/bin/codex")
        self.assertEqual(plan.store, "/run/ensemble/codex-store")
        self.assertEqual(launch.env["PYTHONPATH"], "/opt/ensemble/src")
        self.assertNotIn(tui.CLIENT_PYTHONPATH, launch.env)
        self.assertNotIn("codex", [call.args[0] for call in which.call_args_list])
        digest = hashlib.sha256(f"{self.work}\x00caller-id".encode()).hexdigest()
        [(path, text)] = json.loads(metadata["runtime_files"]).items()
        self.assertEqual(path, f"/run/ensemble/codex-store/{digest}.json")
        self.assertEqual(
            (json.loads(text)["workdir"], json.loads(text)["sid"]), (str(self.work), "caller-id")
        )

    def test_history_scan_and_observation_never_follow_directory_or_file_links(self):
        self.prepare()
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        path = self.rollout()
        outside = self.root / "outside"
        outside.mkdir()
        moved = outside / path.name
        path.rename(moved)
        path.symlink_to(moved)
        with self.assertRaises(common.RecoveryError):
            common.session_file(
                self.common.store.load(str(self.work), self.spec.sid).snapshot, NATIVE
            )
        path.unlink()
        path.parent.rmdir()
        path.parent.symlink_to(outside, target_is_directory=True)
        self.assertEqual(
            common.session_paths(self.common.store.load(str(self.work), self.spec.sid).snapshot), []
        )

    def launch(self, *, resume=False, client=None):
        run = (client or self.client).tui()
        with (
            mock.patch("shutil.which", return_value="/protected/codex"),
            mock.patch("subprocess.Popen") as proc,
        ):
            launch = run.launch(
                dataclasses.replace(self.spec, prompt="quoted ' prompt\nnext", resume=resume), HOOK
            )
        proc.assert_not_called()
        run.commit()
        return run, launch, json.loads(launch.argv[-1])

    def test_each_hook_label_maps_to_its_hook_kind(self):
        self.assertEqual(
            CodexTui.hooks,
            {
                "SessionStart": HookKind.ACTIVITY,
                "UserPromptSubmit": HookKind.TURN_STARTED,
                "PostToolUse": HookKind.ACTIVITY,
                "PermissionRequest": HookKind.BLOCKED,
                "PreToolUse": HookKind.BLOCKED,
                "Stop": HookKind.TURN_END,
                "Interrupt": HookKind.TURN_END,
            },
        )
        for kind in CodexTui.hooks.values():
            self.assertIs(type(kind), HookKind)

    def test_grants_over_interpreter_package_and_hook_dirs_launch(self):
        dirs = (
            sysconfig.get_path("stdlib"),
            sysconfig.get_path("purelib"),
            os.path.dirname(sys.executable),
            *(os.path.join(root, "ensemble") for root in PACKAGE_ROOTS),
            "/protected",
        )
        for index, path in enumerate(dirs):
            spec = dataclasses.replace(
                self.spec,
                sid=f"granted-{index}",
                access=dataclasses.replace(self.spec.access, dirs=(path,)),
            )
            with (
                self.subTest(path=path),
                mock.patch("shutil.which", return_value="/protected/codex"),
            ):
                committed_tui(self.client, spec, HOOK)
            self.assertIn(
                os.path.realpath(path), self.common.load(str(self.work), spec.sid).snapshot.dirs
            )

    def test_resume_with_a_saved_library_grant_launches(self):
        library = sysconfig.get_path("stdlib")
        spec = dataclasses.replace(
            self.spec,
            access=dataclasses.replace(self.spec.access, dirs=(library,)),
        )
        self.common.save(self.common.prepare(spec))
        self.common.bind(str(self.work), "caller-id", NATIVE)
        self.rollout()
        with mock.patch("shutil.which", return_value="/protected/codex"):
            launch = committed_tui(
                self.client, dataclasses.replace(spec, resume=True, access=self.spec.access), HOOK
            )
        self.assertEqual(json.loads(launch.argv[-1])["selection"], ["resume", NATIVE])
        self.assertIn(
            os.path.realpath(library), self.common.load(str(self.work), spec.sid).snapshot.dirs
        )

    def test_launch_preserves_opaque_hook_words(self):
        for index, hook in enumerate(
            (
                (*HOOK, "extra opaque word"),
                (*HOOK[:-1], json.dumps(list(PACKAGE_ROOTS), indent=2, ensure_ascii=False)),
                (*HOOK[:-1], "not JSON at all"),
            )
        ):
            spec = dataclasses.replace(self.spec, sid=f"opaque-{index}")
            with (
                self.subTest(hook=hook),
                mock.patch("shutil.which", return_value="/protected/codex"),
            ):
                launch = committed_tui(self.client, dataclasses.replace(spec, prompt="hi"), hook)
            data = json.loads(launch.argv[-1])
            self.assertNotIn("import_roots", data)
            self.assertEqual(data["hook"], list(hook))
            plan = tui.decode(data)
            for label, command in tui.commands(plan.hook).items():
                self.assertEqual(shlex.split(command), [*hook, label])

    def payload(self, **fields):
        return {"session_id": NATIVE, "cwd": str(self.cwd), **fields}

    def test_factory_launch_flags_and_opaque_hook(self):
        self.assertIsNot(self.client.tui(), self.client.tui())
        run, launch, data = self.launch()
        self.assertEqual(
            launch.argv[1:5], ("-P", "-s", "-m", "ensemble.session.clients.codex.tui.start")
        )
        plan = tui.decode(data)
        argv = tui.execution_argv(plan, run.state.snapshot, "hooks.state={trusted}")
        self.assertEqual(argv[:4], ["/protected/codex", "--no-daemon", "-C", str(self.cwd)])
        self.assertEqual(argv[-2:], ["--", "quoted ' prompt\nnext"])
        self.assertIn('tui.notification_method="bel"', argv)
        self.assertNotIn("sandbox_workspace_write.writable_roots=[]", argv)
        self.assertIn("model_reasoning_effort=low", argv)
        self.assertIn("gpt-test", argv)
        self.assertEqual(data["hook"], list(HOOK))
        self.assertEqual(plan.hook.argv, HOOK)
        commands = tui.commands(plan.hook)
        self.assertEqual(set(commands), set(run.hooks))
        for label, command in commands.items():
            self.assertEqual(shlex.split(command), [*HOOK, label])
        self.assertEqual(
            run.hook_callback(),
            HookCallback(tui.__name__, {"store": data["store"], "start": 0, "name": ""}),
        )
        self.assertIsNone(self.common.load(str(self.work), self.spec.sid).native_id)

    def test_resume_uses_shared_saved_policy_and_native_file(self):
        self.spec = dataclasses.replace(
            self.spec,
            flags=("-s", "workspace-write", "-c", "sandbox_workspace_write.network_access=false"),
        )
        saved = CodexClient(self.client.config).common.save(
            CodexClient(self.client.config).common.prepare(self.spec)
        )
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        self.rollout()
        other = CodexClient({"env": {"CODEX_HOME": str(self.extra)}})
        run, launch, data = self.launch(resume=True, client=other)
        argv = tui.execution_argv(tui.decode(data), run.state.snapshot, "hooks.state={trusted}")
        self.assertEqual(argv[:3], ["/protected/codex", "resume", NATIVE])
        self.assertIn("gpt-test", argv)
        self.assertIn("sandbox_workspace_write.network_access=false", argv)
        self.assertEqual(launch.env["CODEX_HOME"], saved.snapshot.codex_home)
        self.assertIn(NATIVE, launch.resume)

    def test_the_session_env_is_last_in_the_clients_env_and_its_resume(self):
        access = dataclasses.replace(self.spec.access, env={"PYTHONPATH": "/a b;c"})
        self.spec = dataclasses.replace(self.spec, access=access)
        client = CodexClient({**self.client.config, "env": {"PYTHONPATH": "/config"}})
        _, launch, _ = self.launch(client=client)
        self.assertEqual(launch.env[tui.CLIENT_PYTHONPATH], "/a b;c")
        self.assertEqual(launch.env["PYTHONPATH"], os.pathsep.join(dict.fromkeys(PACKAGE_ROOTS)))
        state = self.common.bind(str(self.work), self.spec.sid, NATIVE)
        self.rollout()
        self.spec = dataclasses.replace(self.spec, access=Access(cwd=self.spec.workdir))
        _, resumed, _ = self.launch(resume=True, client=client)
        self.assertEqual(resumed.env[tui.CLIENT_PYTHONPATH], "/a b;c")
        self.assertNotIn("/a b;c", resumed.resume)
        self.assertEqual(self.common.resume_launch(state).env["PYTHONPATH"], "/a b;c")

    def test_no_env_value_reaches_the_argv_new_or_resumed(self):
        secrets = ("sk-fake-config", "sk-fake-access", "/sk-fake-path")
        client = CodexClient({**self.client.config, "env": {"CFG_TOKEN": secrets[0]}})
        access = dataclasses.replace(self.spec.access, env={"ACCESS_TOKEN": secrets[1]})
        self.spec = dataclasses.replace(self.spec, access=access, env={"PYTHONPATH": secrets[2]})
        _, launch, _ = self.launch(client=client)
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        self.rollout()
        _, resumed, _ = self.launch(resume=True, client=client)
        for each in (launch, resumed):
            argv = "\0".join(each.argv)
            for secret in secrets:
                self.assertNotIn(secret, argv)
            self.assertEqual(each.env[tui.CLIENT_PYTHONPATH], secrets[2])

    def test_invalid_launch_plan_saves_no_recovery_state(self):
        with (
            mock.patch("shutil.which", return_value="/protected/codex"),
            self.assertRaisesRegex(ConfigError, "invalid prompt"),
        ):
            committed_tui(
                self.client, dataclasses.replace(self.spec, prompt="invalid\0prompt"), HOOK
            )
        self.assertIsNone(self.common.load(str(self.work), self.spec.sid))
        self.assertFalse((self.home / ".ensemble").exists())

    def test_invalid_launch_inputs_leave_no_state_directory(self):
        for prompt, hook, roots in (
            ("surrogate\ud800", HOOK, PACKAGE_ROOTS),
            ("hi", (), PACKAGE_ROOTS),
            ("hi", ("relative-hook",), PACKAGE_ROOTS),
        ):
            with (
                self.subTest(prompt=prompt, hook=hook, roots=roots),
                mock.patch("shutil.which", return_value="/protected/codex"),
                self.assertRaises(ConfigError),
            ):
                committed_tui(self.client, dataclasses.replace(self.spec, prompt=prompt), hook)
            self.assertIsNone(self.common.load(str(self.work), self.spec.sid))
            self.assertFalse((self.home / ".ensemble").exists())

    def test_resume_failure_happens_before_command(self):
        with (
            mock.patch("shutil.which") as which,
            self.assertRaisesRegex(ConfigError, "missing.*state"),
        ):
            committed_tui(
                self.client, dataclasses.replace(self.spec, prompt="again", resume=True), HOOK
            )
        which.assert_not_called()

    def test_interactive_environment_preserves_external_default_values(self):
        parent = {key: value for key, value in os.environ.items() if key not in self.common.UNSET}
        for markers, color in (({"CLAUDECODE": "1"}, "1"), ({"CODEX_THREAD_ID": NATIVE}, "0")):
            with (
                self.subTest(markers=markers),
                mock.patch.dict(
                    os.environ,
                    {**parent, **markers, "NO_COLOR": color, "PAGER": "less"},
                    clear=True,
                ),
            ):
                self.spec = dataclasses.replace(self.spec, sid=f"color-{color}")
                _, launch, _ = self.launch()
                self.assertNotIn("NO_COLOR", launch.unset)
                self.assertNotIn("PAGER", launch.unset)

    def test_interactive_environment_injects_no_defaults_and_drops_parent_markers(self):
        with mock.patch.dict(
            os.environ, {"CODEX_CI": "1", "NO_COLOR": "1", "CODEX_THREAD_ID": NATIVE}
        ):
            _, launch, _ = self.launch()
        self.assertNotIn("CODEX_CI", launch.env)
        self.assertNotIn("NO_COLOR", launch.env)
        self.assertEqual(launch.unset, (*self.common.UNSET, tui.CLIENT_PYTHONPATH))
        self.assertIn("CODEX_THREAD_ID", launch.unset)
        second = dataclasses.replace(self.spec, sid="explicit-env", tier=None, effort=None)
        client = CodexClient({"env": {"CODEX_CI": "configured", "PAGER": "less"}})
        with mock.patch("shutil.which", return_value="/protected/codex"):
            launch = committed_tui(client, dataclasses.replace(second, prompt="hi"), HOOK)
        self.assertEqual(launch.env["CODEX_CI"], "configured")
        self.assertNotIn("CODEX_CI", launch.unset)
        self.assertEqual(launch.env["PAGER"], "less")

    def test_interactive_launch_clears_git_local_environment_in_sorted_order(self):
        inherited = {key: "inherited" for key in LOCAL_ENV_VARS}
        with mock.patch.dict(os.environ, inherited):
            _, launch, _ = self.launch()
            snapshot = self.common.load(str(self.work), self.spec.sid).snapshot
            effective = self.common.environment(snapshot, interactive=True, ambient=True)
            child = {key: value for key, value in os.environ.items() if key not in launch.unset}
            child.update(launch.env)
        self.assertEqual(launch.unset[: len(LOCAL_ENV_VARS)], tuple(sorted(LOCAL_ENV_VARS)))
        self.assertFalse(LOCAL_ENV_VARS.intersection(launch.env))
        self.assertFalse(LOCAL_ENV_VARS.intersection(effective))
        self.assertFalse(LOCAL_ENV_VARS.intersection(child))

    def test_hook_mapping_null_reply_and_submit(self):
        run, _, _ = self.launch()
        self.assertEqual(run.submit("x"), (Paste("x"), Key("Enter")))
        self.assertEqual(
            run.hook_events("SessionStart", self.payload()), [ClientBound(NATIVE), Activity("")]
        )
        self.assertEqual(
            run.hook_events("PostToolUse", self.payload(tool_name="Bash")), [Activity("Bash")]
        )
        self.assertEqual(
            run.hook_events("PermissionRequest", self.payload(tool_name="Bash")), [Blocked("Bash")]
        )
        questions = [{"header": "Scope", "question": "Which?"}, {"question": "And?"}, "x"]
        self.assertEqual(
            run.hook_events(
                "PreToolUse",
                self.payload(tool_name="request_user_input", tool_input={"questions": questions}),
            ),
            [Blocked("question: Scope; And?")],
        )
        for tool_input in (None, "x", {"questions": None}, {"questions": 3}, {"questions": "ab"}):
            with self.subTest(tool_input=tool_input):
                self.assertEqual(
                    run.hook_events(
                        "PreToolUse",
                        self.payload(tool_name="request_user_input", tool_input=tool_input),
                    ),
                    [Blocked("question")],
                )
        self.assertEqual(
            run.hook_events("Stop", self.payload(last_assistant_message=None)), [TurnEnd()]
        )
        for fields in (
            {"agent_id": CHILD},
            {"session_id": "invalid"},
            {"session_id": CHILD},
            {"cwd": str(self.extra)},
        ):
            self.assertEqual(run.hook_events("Stop", self.payload(**fields)), [])
        self.assertEqual(run.hook_events("unknown", self.payload()), [])

    def test_a_stop_reports_no_background_work(self):
        run, _, _ = self.launch()
        self.assertEqual(
            run.hook_events(
                "Stop", self.payload(last_assistant_message="raw", background_tasks=[1])
            ),
            [ClientBound(NATIVE), TurnEnd("raw", 0)],
        )
        for payload in ({"background_tasks": [1, {}]}, {}, self.payload()):
            with self.subTest(payload=payload):
                self.assertEqual(run.pending(payload), 0)

    def test_interrupt_ends_its_turn_without_the_previous_turns_text(self):
        run = self.client.tui()
        self.assertEqual(
            run.hook_events("Interrupt", self.payload()), [ClientBound(NATIVE), TurnEnd()]
        )
        self.assertEqual(
            run.hook_events("Stop", self.payload(last_assistant_message="kept")),
            [TurnEnd("kept")],
        )
        self.assertEqual(
            run.hook_events("Interrupt", self.payload(last_assistant_message="discard")),
            [TurnEnd()],
        )
        self.assertEqual(
            run.hook_events("Stop", self.payload(last_assistant_message="after")),
            [TurnEnd("after")],
        )
        attached = self.client.tui()
        self.assertEqual(
            attached.replay(
                [
                    ("Stop", self.payload(last_assistant_message="kept")),
                    ("UserPromptSubmit", self.payload()),
                    ("Interrupt", self.payload()),
                ]
            ),
            [ClientBound(NATIVE), TurnEnd("kept"), TurnEnd()],
        )
        self.assertEqual(attached.hook_events("Interrupt", self.payload()), [TurnEnd()])

    def test_restore_readies_a_fresh_tui_as_the_one_that_launched(self):
        run, _, data = self.launch()
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        foreign = self.payload(cwd=str(self.extra))
        self.assertEqual(
            self.client.tui().hook_events("Stop", foreign), [ClientBound(NATIVE), TurnEnd()]
        )
        restored = self.client.tui()
        restored.restore(self.spec, run.hook_callback().state)
        self.assertEqual(
            (restored.native, restored.store, restored.state),
            (NATIVE, data["store"], self.common.load(str(self.work), self.spec.sid)),
        )
        self.assertEqual(restored.hook_callback(), run.hook_callback())
        for payload in (foreign, self.payload(session_id=CHILD)):
            self.assertEqual(restored.hook_events("Stop", payload), [])
        self.assertEqual(
            restored.hook_events("Stop", self.payload(last_assistant_message="r")),
            [ClientBound(NATIVE), TurnEnd("r")],
        )

    def test_restore_without_a_recovery_state_keeps_none_and_a_bad_store_is_a_config_error(self):
        self.launch()
        fresh = self.client.tui()
        fresh.restore(dataclasses.replace(self.spec, sid="other-id"), {})
        self.assertEqual((fresh.state, fresh.native), (None, ""))
        not_a_directory = self.extra / "store"
        not_a_directory.write_text("")
        for state in ({"store": "relative/store"}, {"store": str(not_a_directory)}):
            with self.subTest(state=state), self.assertRaises(ConfigError):
                self.client.tui().restore(self.spec, state)

    def test_restore_rejects_a_bad_transcript_cursor(self):
        self.launch()
        for state in ({"start": -1}, {"start": "0"}, {"start": True}):
            with self.subTest(state=state), self.assertRaises(ConfigError):
                self.client.tui().restore(self.spec, state)

    def bound(self):
        """A launched tui bound to NATIVE by its SessionStart, and its native session file."""
        run, _, _ = self.launch()
        self.assertEqual(run.observe(), [])
        path = self.rollout()
        run.hook_events("SessionStart", self.payload())
        return run, path

    def test_observe_reports_a_mid_turn_reply_once_and_not_the_stops_again(self):
        run, path = self.bound()
        _append(path, _said("user", "go"), _said("assistant", "[ensemble-progress:start] x"), TOOL)
        self.assertEqual(run.observe(), [Reply("[ensemble-progress:start] x")])
        self.assertEqual(
            run.hook_events("Stop", self.payload(last_assistant_message="done")),
            [TurnEnd("done")],
        )
        _append(path, _said("assistant", "done"))
        self.assertEqual(run.observe(), [])
        _append(path, _said("user", "again"), _said("assistant", "done"))
        self.assertEqual(run.observe(), [Reply("done")])

    def test_a_stop_repeating_the_observed_reply_leaves_nothing_to_report(self):
        run, path = self.bound()
        _append(path, _said("user", "go"), _said("assistant", "done"))
        self.assertEqual(run.observe(), [Reply("done")])
        self.assertEqual(
            run.hook_events("Stop", self.payload(last_assistant_message="done")),
            [TurnEnd("done")],
        )
        self.assertEqual(run.observe(), [])
        _append(path, _said("assistant", "done"))
        self.assertEqual(run.observe(), [Reply("done")])

    def test_observe_skips_records_that_are_not_assistant_text(self):
        run, path = self.bound()
        with path.open("a", encoding="utf-8") as stream:
            stream.write("not json\n[]\n")
        _append(
            path,
            _said("developer", "rules"),
            _said("assistant", " "),
            {"type": "response_item", "payload": {"type": "message", "role": "assistant"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "dup"}},
            {"type": "response_item", "payload": None},
            _said("assistant", "a"),
        )
        self.assertEqual(run.observe(), [Reply("a")])

    def test_observe_joins_a_messages_text_blocks(self):
        run, path = self.bound()
        _append(path, _said("assistant", "a", "b"))
        self.assertEqual(run.observe(), [Reply("a\nb")])

    def test_observe_reports_a_failed_turn_after_its_replies(self):
        run, path = self.bound()
        _append(path, _said("user", "go"), _said("assistant", "a"), _failed("bad request"))
        self.assertEqual(run.observe(), [Reply("a"), TurnFailed("other", "bad request")])

    def test_unhooked_initial_and_pane_turns_fail_once(self):
        run, path = self.bound()
        for turn in ("first", "pane-followup"):
            with self.subTest(turn=turn):
                started = {
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": turn},
                }
                failed = _failed("Please try again", "server_overloaded")
                failed["payload"]["turn_id"] = turn
                _append(path, started, failed, failed)
                self.assertEqual(
                    run.observe(),
                    [TurnStarted(), TurnFailed("server_overloaded", "Please try again")],
                )
                self.assertEqual(run.observe(), [])
                self.assertEqual(run.hook_events("Stop", self.payload(turn_id=turn)), [])

    def test_native_failure_can_arrive_before_its_accepted_input_hook(self):
        run, path = self.bound()
        for turn in ("first", "pane-followup"):
            failed = _failed("try again", "server_overloaded")
            failed["payload"]["turn_id"] = turn
            _append(path, failed)
            self.assertEqual(
                run.observe(), [TurnStarted(), TurnFailed("server_overloaded", "try again")]
            )
            self.assertEqual(run.hook_events("UserPromptSubmit", self.payload(turn_id=turn)), [])

    def test_idless_hook_cannot_overwrite_an_identified_native_turn(self):
        run, path = self.bound()
        _append(path, {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "one"}})
        self.assertEqual(run.observe(), [TurnStarted()])
        self.assertEqual(run.hook_events("UserPromptSubmit", self.payload()), [])
        failed = _failed("retry", "server_overloaded")
        failed["payload"]["turn_id"] = "one"
        _append(path, failed)
        self.assertEqual(run.observe(), [TurnFailed("server_overloaded", "retry")])
        self.assertEqual(run.hook_events("Stop", self.payload()), [])

    def test_native_start_upgrades_an_anonymous_accepted_turn(self):
        run, path = self.bound()
        self.assertEqual(run.hook_events("UserPromptSubmit", self.payload()), [TurnStarted()])
        _append(path, {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "one"}})
        self.assertEqual(run.observe(), [])
        self.assertEqual(
            run.hook_events("Stop", self.payload(turn_id="one", last_assistant_message="done")),
            [TurnEnd("done")],
        )

    def test_native_terminal_upgrades_an_anonymous_accepted_turn(self):
        run, path = self.bound()
        self.assertEqual(run.hook_events("UserPromptSubmit", self.payload()), [TurnStarted()])
        failed = _failed("retry", "server_overloaded")
        failed["payload"]["turn_id"] = "one"
        _append(path, failed)
        self.assertEqual(run.observe(), [TurnFailed("server_overloaded", "retry")])
        self.assertEqual(run.hook_events("Stop", self.payload()), [])

    def test_native_and_hook_acceptance_share_identity_and_old_stop_cannot_end_next_turn(self):
        run, path = self.bound()
        self.assertEqual(
            run.hook_events("UserPromptSubmit", self.payload(turn_id="one")), [TurnStarted()]
        )
        _append(path, {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "one"}})
        self.assertEqual(run.observe(), [])
        self.assertEqual(
            run.hook_events("Stop", self.payload(turn_id="one", last_assistant_message="one")),
            [TurnEnd("one")],
        )
        self.assertEqual(
            run.hook_events("UserPromptSubmit", self.payload(turn_id="two")), [TurnStarted()]
        )
        self.assertEqual(
            run.hook_events("Stop", self.payload(turn_id="one", last_assistant_message="old")), []
        )
        self.assertEqual(
            run.hook_events("Stop", self.payload(last_assistant_message="ambiguous")), []
        )
        self.assertEqual(
            run.hook_events("Stop", self.payload(turn_id="two", last_assistant_message="two")),
            [TurnEnd("two")],
        )

    def test_replay_does_not_emit_new_turn_starts(self):
        run, path = self.bound()
        _append(path, {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "one"}})
        found = run.replay([("UserPromptSubmit", self.payload(turn_id="one"))])
        self.assertFalse(any(isinstance(event, TurnStarted) for event in found))

    def test_native_complete_delivers_reply_before_a_later_turn_can_overtake_hook(self):
        run, path = self.bound()
        _append(
            path,
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "one"}},
            _said("assistant", "complete\n\nreply"),
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "one",
                    "last_agent_message": "complete\n\nreply",
                },
            },
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "two"}},
        )
        self.assertEqual(
            run.observe(),
            [
                TurnStarted(),
                Reply("complete\n\nreply"),
                TurnEnd("complete\n\nreply"),
                TurnStarted(),
            ],
        )
        self.assertEqual(
            run.hook_events(
                "Stop", self.payload(turn_id="one", last_assistant_message="complete\n\nreply")
            ),
            [],
        )

    def test_observe_leaves_a_usage_limit_to_the_screen(self):
        run, path = self.bound()
        for message in (
            "You've hit your usage limit. Try again at 3:00 PM.",
            "You’ve hit your usage limit.",
        ):
            _append(path, _failed(message, "usage_limit_exceeded"))
            self.assertEqual(run.observe(), [])
        _append(path, _failed("Usage limit reached.", "usage_limit_exceeded"))
        self.assertEqual(
            run.observe(), [TurnFailed("usage_limit_exceeded", "Usage limit reached.")]
        )

    def test_observe_reports_a_failed_turn_once_and_none_before_the_cursor(self):
        self.prepare()
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        path = self.rollout()
        _append(path, _said("user", "old"), _failed("old failure"))
        run, _, _ = self.launch(resume=True)
        self.assertEqual(run.observe(), [])
        _append(path, _said("user", "new"), _failed("new failure"))
        self.assertEqual(run.observe(), [TurnFailed("other", "new failure")])
        self.assertEqual(run.observe(), [])

    def test_observe_waits_for_a_whole_line(self):
        run, path = self.bound()
        line = json.dumps(_said("assistant", "a"))
        with path.open("a", encoding="utf-8") as stream:
            stream.write(line[:10])
        self.assertEqual(run.observe(), [])
        with path.open("a", encoding="utf-8") as stream:
            stream.write(line[10:] + "\n")
        self.assertEqual(run.observe(), [Reply("a")])

    def test_observe_looks_the_file_up_at_most_every_few_seconds(self):
        run, _, _ = self.launch()
        run.hook_events("SessionStart", self.payload())
        with (
            mock.patch("time.monotonic", return_value=1000.0) as clock,
            mock.patch.object(run.common, "transcript", wraps=run.common.transcript) as lookup,
        ):
            for _ in range(5):
                self.assertEqual(run.observe(), [])
            self.assertEqual(lookup.call_count, 1)
            path = self.rollout()
            _append(path, _said("assistant", "late"))
            clock.return_value += tui.LOOKUP_EVERY - 0.1
            self.assertEqual(run.observe(), [])
            lookup.side_effect = ConfigError("duplicate")
            clock.return_value += 0.1
            self.assertEqual((run.observe(), run.observe()), ([], []))
            self.assertEqual(lookup.call_count, 2)
            lookup.side_effect = None
            clock.return_value += tui.LOOKUP_EVERY
            self.assertEqual(run.observe(), [Reply("late")])
            clock.return_value += tui.LOOKUP_EVERY
            _append(path, _said("assistant", "next"))
            self.assertEqual(run.observe(), [Reply("next")])
            self.assertEqual(lookup.call_count, 3)

    def test_a_turn_end_looks_a_backed_off_file_up_before_its_turn_end(self):
        run, _, _ = self.launch()
        run.hook_events("SessionStart", self.payload())
        with mock.patch("time.monotonic", return_value=1000.0) as clock:
            self.assertEqual(run.observe(), [])
            _append(self.rollout(), _said("user", "go"), _said("assistant", "A"))
            clock.return_value += 0.5
            self.assertEqual(run.observe(), [])
            self.assertEqual(
                run.hook_events("Stop", self.payload(last_assistant_message="A")),
                [Reply("A"), TurnEnd("A")],
            )
            clock.return_value += tui.LOOKUP_EVERY
            self.assertEqual(run.observe(), [])

    def test_resume_starts_the_cursor_at_the_session_files_end(self):
        self.prepare()
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        path = self.rollout()
        _append(path, _said("assistant", "old"))
        run, launch, _ = self.launch(resume=True)
        self.assertEqual(launch.transcript, str(path))
        self.assertEqual(run.hook_callback().state["start"], path.stat().st_size)
        self.assertEqual(run.observe(), [])
        _append(path, _said("assistant", "new"))
        self.assertEqual(run.observe(), [Reply("new")])

    def attached(self, *labels):
        """A tui attached after the launching one read prompt "go" and reply "a", its session file
        and its replay of a hook log of (label, Stop text) `labels`.
        """
        run, path = self.bound()
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        _append(path, _said("user", "go"), _said("assistant", "a"))
        self.assertEqual(run.observe(), [Reply("a")])
        attached = self.client.tui()
        attached.restore(self.spec, run.hook_callback().state)
        records = [
            (label, self.payload(last_assistant_message=text)) if text else (label, self.payload())
            for label, text in labels
        ]
        return attached, path, attached.replay(records)

    def test_attach_replays_the_session_file_then_the_hook_log(self):
        _, _, replayed = self.attached(*TURNS)
        self.assertEqual(
            replayed,
            [
                Reply("a"),
                ClientBound(NATIVE),
                Activity(""),
                TurnEnd("a"),
                TurnEnd("final"),
            ],
        )

    def test_attach_reads_on_from_the_session_files_end(self):
        attached, path, _ = self.attached(*TURNS)
        self.assertEqual(attached.observe(), [])
        _append(path, _said("user", "next"), _said("assistant", "b"))
        self.assertEqual(attached.observe(), [Reply("b")])

    def test_attach_a_new_prompt_clears_the_pending_stop_text(self):
        attached, path, _ = self.attached(*TURNS)
        _append(path, _said("user", "next"), _said("assistant", "final"))
        self.assertEqual(attached.observe(), [Reply("final")])

    def test_attach_mid_turn_skips_the_pending_stop_text_once(self):
        attached, path, replayed = self.attached(("UserPromptSubmit", ""), ("Stop", "final"))
        self.assertEqual(replayed, [Reply("a"), ClientBound(NATIVE), TurnEnd("final")])
        _append(path, _said("assistant", "final"), _said("assistant", "final"))
        self.assertEqual(attached.observe(), [Reply("final")])

    def test_identity_accepts_canonical_cwd_alias_and_keeps_native_binding(self):
        run, _, _ = self.launch()
        alias = self.root / "cwd-alias"
        alias.symlink_to(self.cwd, target_is_directory=True)
        self.assertEqual(run.session_id(self.payload(cwd=str(alias))), NATIVE)
        self.assertEqual(run.session_id(self.payload(session_id=CHILD, cwd=str(alias))), "")
        self.assertEqual(run.native, NATIVE)

    def test_native_mismatch_is_rejected_before_canonical_cwd_check(self):
        run, _, _ = self.launch()
        self.assertEqual(run.session_id(self.payload()), NATIVE)
        with mock.patch("os.path.realpath", side_effect=RuntimeError("unexpected cwd check")):
            self.assertEqual(run.session_id(self.payload(session_id=CHILD)), "")
        self.assertEqual(run.native, NATIVE)

    def test_invalid_identity_does_not_bind_or_update_the_reply(self):
        run = self.client.tui()
        for fields in (
            {"agent_id": CHILD},
            {"session_id": None},
            {"session_id": "invalid"},
            {"cwd": None},
            {"cwd": 1},
            {"cwd": "relative"},
            {"cwd": "/invalid\0cwd"},
        ):
            with self.subTest(fields=fields):
                self.assertEqual(
                    run.session_id(
                        self.payload(
                            hook_event_name="Stop", last_assistant_message="foreign", **fields
                        )
                    ),
                    "",
                )
                self.assertEqual((run.native, run.last_reply), ("", ""))

    def test_limit_screen_official_variants_and_reset(self):
        run = self.client.tui()
        run.common.config["env"] = {"TZ": "UTC"}
        stamp = datetime(2027, 1, 2, 3, 4, tzinfo=ZoneInfo("UTC")).timestamp()
        for prefix in (
            "You’ve hit your usage limit.",
            "You've hit your usage limit. Visit https://chatgpt.com/settings/usage "
            "to purchase more credits or",
            "Quota exceeded. Check your plan and billing details.",
            "Your workspace is out of credits.",
        ):
            with self.subTest(prefix=prefix):
                self.assertEqual(
                    run.limit("■ " + prefix + " Try again at Jan 2nd, 2027 3:04 AM.\n› "),
                    LimitHit(stamp),
                )
        self.assertEqual(run.limit("■ You’ve hit your usage limit. Try again later."), LimitHit())
        self.assertEqual(
            run.limit("■ You've hit your usage limit. Try again at Jan 2th, 2027 3:04 AM."),
            LimitHit(),
        )
        self.assertIsNone(run.limit("assistant: You've hit your usage limit. is a quoted example"))
        self.assertIsNone(run.limit("ordinary network error"))

    def test_writable_executable_and_hook_dependency_launch(self):
        for path in (self.cwd / "codex", self.extra / "driver.py"):
            spec = dataclasses.replace(self.spec, sid="writable-" + path.name)
            hook = HOOK if path.name == "codex" else (*HOOK[:2], str(path), *HOOK[3:])
            with mock.patch(
                "shutil.which",
                return_value=str(path if path.name == "codex" else "/protected/codex"),
            ):
                launch = committed_tui(self.client, dataclasses.replace(spec, prompt="hi"), hook)
            data = json.loads(launch.argv[-1])
            self.assertEqual(
                (data["executable"], data["hook"]),
                (str(path) if path.name == "codex" else "/protected/codex", list(hook)),
            )
            self.assertIsNotNone(self.common.load(str(self.work), spec.sid))

    def test_a_wrapped_or_driver_flattened_limit_screen_keeps_its_header_and_reset(self):
        client = CodexClient({"env": {"TZ": "UTC"}})
        stamp = datetime(2027, 1, 2, 3, 4, tzinfo=ZoneInfo("UTC")).timestamp()
        for screen in (
            "■ You hit your spend cap set by the owner\n  of your workspace. Try again at Jan\n  2nd, 2027 3:04 AM.\n› ",
            "Codex > gpt-test ■ You've hit your usage limit. Try again at Jan 2nd, 2027 3:04 AM. › ",
        ):
            with self.subTest(screen=screen):
                self.assertEqual(client.tui().limit(screen), LimitHit(stamp))

    def test_codex_has_no_opening(self):
        self.assertIsNone(self.client.tui().opening)

    def test_each_dialog_shows_on_its_own_screen(self):
        run = self.client.tui()
        self.assertEqual(set(DIALOG_SCREENS), set(run.dialogs))
        for dialog, screen in DIALOG_SCREENS.items():
            with self.subTest(dialog=dialog):
                self.assertEqual([d for d in run.dialogs if run.shows(d, screen)], [dialog])

    def test_a_dialog_title_in_the_agents_output_is_no_dialog(self):
        run = self.client.tui()
        for dialog in run.dialogs:
            screens = (
                f"• Codex showed {dialog} at startup.\n\n› Ask Codex to do anything",
                f"• Startup dialogs:\n\n  {dialog}\n\n› Ask Codex to do anything",
            )
            for screen in screens:
                with self.subTest(dialog=dialog, screen=screen):
                    self.assertFalse(run.shows(dialog, screen))

    def test_assistant_bullets_and_quoted_limit_text_are_not_native_errors(self):
        run = self.client.tui()
        for screen in (
            (
                "Codex > gpt-test • You have asked me to quote this error. • You've hit your usage limit. is a quoted example › "
            ),
            "• You've hit your usage limit. is a quoted example\n› ",
            "You've hit your usage limit. is a quoted example",
            "! You've hit your usage limit. is a quoted example",
            "✖ You've hit your usage limit. is a quoted example",
        ):
            with self.subTest(screen=screen):
                self.assertIsNone(run.limit(screen))

    def test_configured_tmpdir_over_dependencies_launches_fresh_or_resumed(self):
        self.prepare()
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        self.rollout()
        for resume in (False, True):
            for index, tmpdir in enumerate(("/protected", "/driver-only", *ROOTS, sys.prefix)):
                with self.subTest(resume=resume, tmpdir=tmpdir):
                    config = {**self.common.config, "env": {"TMPDIR": tmpdir}}
                    client = CodexClient(config)
                    spec = (
                        self.spec if resume else dataclasses.replace(self.spec, sid=f"env-{index}")
                    )
                    with mock.patch("shutil.which", return_value="/protected/codex"):
                        launch = committed_tui(
                            client,
                            dataclasses.replace(spec, prompt="hi", resume=resume),
                            (*HOOK[:2], "/driver-only/hook.py", *HOOK[3:]),
                        )
                    if resume:
                        self.assertNotIn("TMPDIR", launch.env)
                    self.assertIsNotNone(self.common.load(str(self.work), spec.sid))
        self.assertEqual(self.common.load(str(self.work), self.spec.sid).native_id, NATIVE)

    def test_tui_client_queries_fresh_saved_and_effective_temp_roots(self):
        home, temporary = self.root / "configured-home", self.root / "configured-tmp"
        home.mkdir()
        temporary.mkdir()
        alias = self.root / "temporary-link"
        alias.symlink_to(temporary)
        client = CodexClient(
            self.common.config | {"env": {"CODEX_HOME": str(home), "TMPDIR": str(alias)}},
        )
        run = client.tui()
        expected = [str(p) for p in (self.cwd, home, self.extra)]
        with mock.patch("subprocess.Popen") as proc:
            roots = run.common.writable_dirs(dataclasses.replace(self.spec, resume=False))
            self.assertEqual(list(roots[:3]), expected)
            self.assertIn(str(temporary), roots)
            self.assertEqual(len(roots), len(set(roots)))
            self.assertFalse((self.home / ".ensemble").exists())
            with mock.patch("shutil.which", return_value="/protected/codex"):
                run.launch(dataclasses.replace(self.spec, prompt="hello"), HOOK)
            run.commit()
            saved = self.common.load(str(self.work), self.spec.sid).writable_roots
            changed_home, changed_tmp = self.root / "changed-home", self.root / "changed-tmp"
            changed = CodexClient(
                self.common.config
                | {"env": {"CODEX_HOME": str(changed_home), "TMPDIR": str(changed_tmp)}},
            ).tui()
            spec = dataclasses.replace(
                self.spec,
                access=dataclasses.replace(self.spec.access, cwd=str(self.extra), dirs=()),
            )
            before = self.record().read_bytes()
            resumed = changed.common.writable_dirs(dataclasses.replace(spec, resume=True))
            self.assertEqual(tuple(resumed[: len(saved)]), saved)
            self.assertNotIn(str(changed_tmp), resumed)
            self.assertNotIn(str(changed_home), resumed)
            self.assertEqual(self.record().read_bytes(), before)
        proc.assert_not_called()


PROMPT = "quoted ' prompt\nnext"

POLICY = (
    "approval_policy",
    "approvals_reviewer",
    "sandbox_workspace_write",
)

REFUSED = (
    ("-C", "/x"),
    ("--cd=/x",),
    ("-C/x",),
    ("-p", "work"),
    ("--profile", "work"),
    ("resume",),
    ("fork",),
    ("--last",),
    ("--dangerously-bypass-hook-trust",),
    ("-c", "hooks.Stop=[]"),
    ("--config", '"hooks".state={}'),
    ("--config=sandbox_workspace_write.network_access=true",),
    ("-csandbox_workspace_write.writable_roots=[]",),
)


class SuppliedFlagsTest(Base):
    def launch(self, spec, *, resume=False, hook=HOOK, permission=Permission.AUTO):
        with mock.patch("shutil.which", return_value="/protected/codex"):
            launch = committed_tui(
                self.client,
                dataclasses.replace(spec, prompt=PROMPT, resume=resume),
                hook,
                permission,
            )
        return launch, json.loads(launch.argv[-1])

    def test_the_configs_flags_reach_the_client_before_the_specs(self):
        own = ("-c", "plugins.x@m.enabled=false")
        self.client = CodexClient({**self.client.config, "flags": list(own)})
        spec = dataclasses.replace(self.spec, flags=("--enable", "web"))
        _, data = self.launch(spec)
        snapshot = self.common.load(data["workdir"], data["sid"]).snapshot
        argv = tui.execution_argv(tui.decode(data), snapshot, "hooks.state={trusted}")
        start = argv.index(own[1]) - 1
        self.assertEqual(argv[start : start + 4], [*own, "--enable", "web"])

    def test_a_resume_reads_the_configs_current_flags_and_the_recorded_ones(self):
        self.client = CodexClient(
            {**self.client.config, "flags": ["-c", "plugins.x@m.enabled=false"]}
        )
        spec = dataclasses.replace(self.spec, flags=("--enable", "web"))
        self.launch(spec)
        self.common.bind(str(self.work), spec.sid, NATIVE)
        self.rollout()
        self.client = CodexClient({**self.client.config, "flags": ["--disable", "apps"]})
        _, data = self.launch(dataclasses.replace(spec, flags=()), resume=True)
        plan = tui.decode(data)
        self.assertEqual(plan.flags, ("--disable", "apps"))
        snapshot = self.common.load(data["workdir"], data["sid"]).snapshot
        self.assertEqual(snapshot.flags, ("--enable", "web"))
        argv = tui.execution_argv(plan, snapshot, "hooks.state={trusted}")
        start = argv.index("--disable")
        self.assertEqual(argv[start : start + 4], ["--disable", "apps", "--enable", "web"])
        self.assertNotIn("plugins.x@m.enabled=false", argv)
        self.assertTrue(holds(tui.trust_args(plan, snapshot), ("--disable", "apps")))

    def test_only_the_runs_permission_modes_flags_reach_codex_and_its_trust_step(self):
        self.client = CodexClient({**self.client.config, "modes": MODES})
        for permission, present, absent in (
            (Permission.AUTO, AUTO, BYPASS),
            (Permission.YOLO, (BYPASS,), "-s"),
        ):
            with self.subTest(permission=permission):
                spec = dataclasses.replace(self.spec, sid=f"run-{permission}")
                _, data = self.launch(spec, permission=permission)
                plan = tui.decode(data)
                self.assertEqual(plan.permission, tuple(MODES[permission]))
                snapshot = self.common.load(data["workdir"], data["sid"]).snapshot
                self.assertEqual(snapshot.flags, ())
                argv = tui.execution_argv(plan, snapshot, "hooks.state={trusted}")
                self.assertTrue(holds(argv, present), argv)
                self.assertNotIn(absent, argv)
                trust = tui.trust_args(plan, snapshot)
                for value in AUTO[3::2]:
                    self.assertEqual(value in trust, permission == Permission.AUTO)

    def test_the_packaged_config_turns_the_ensemble_plugin_off(self):
        self.client = packaged()
        _, data = self.launch(self.spec)
        snapshot = self.common.load(data["workdir"], data["sid"]).snapshot
        argv = tui.execution_argv(tui.decode(data), snapshot, "hooks.state={trusted}")
        start = argv.index(PLUGIN_OFF[1]) - 1
        self.assertEqual(argv[start : start + 2], list(PLUGIN_OFF))

    def test_the_packaged_config_turns_the_browser_computer_use_apps_and_suggestions_off(self):
        self.client = packaged()
        _, data = self.launch(self.spec)
        snapshot = self.common.load(data["workdir"], data["sid"]).snapshot
        argv = tui.execution_argv(tui.decode(data), snapshot, "hooks.state={trusted}")
        for pair in TOOLS_OFF:
            with self.subTest(pair=pair):
                self.assertTrue(holds(argv, pair), argv)
                self.assertEqual(argv.count(pair[1]), 1)

    def test_the_packaged_config_gives_a_crew_worker_each_codex_policy_flag_once(self):
        self.client = packaged()
        _, data = self.launch(self.spec)
        snapshot = self.common.load(data["workdir"], data["sid"]).snapshot
        argv = tui.execution_argv(tui.decode(data), snapshot, "hooks.state={trusted}")
        for flag, value in UNIFIED:
            with self.subTest(value=value):
                self.assertEqual(argv.count(value), 1)
                self.assertEqual(argv[argv.index(value) - 1], flag)
        self.assertEqual(argv.count("-s"), 1)

    def test_the_mcp_off_flag_reaches_codex_and_its_trust_step_expanded(self):
        (self.codex_home / "config.toml").write_text('[mcp_servers.t]\ncommand = "t"\n')
        _, data = self.launch(dataclasses.replace(self.spec, flags=("-c", "mcp_servers={}")))
        plan = tui.decode(data)
        snapshot = self.common.load(data["workdir"], data["sid"]).snapshot
        off = ["-c", "mcp_servers.t.enabled=false", "-c", 'mcp_servers.t.command="t"']
        for argv in (
            tui.execution_argv(plan, snapshot, "hooks.state={trusted}"),
            tui.trust_args(plan, snapshot),
        ):
            with self.subTest(argv=argv):
                self.assertTrue(holds(argv, off), argv)
                self.assertNotIn("mcp_servers={}", argv)

    def test_the_mcp_off_flag_in_the_configs_flags_is_expanded_too(self):
        (self.codex_home / "config.toml").write_text('[mcp_servers.t]\ncommand = "t"\n')
        self.client = CodexClient({**self.client.config, "flags": ["-c", "mcp_servers={}"]})
        _, data = self.launch(self.spec)
        plan = tui.decode(data)
        snapshot = self.common.load(data["workdir"], data["sid"]).snapshot
        self.assertEqual((plan.flags, snapshot.flags), (("-c", "mcp_servers={}"), ()))
        off = ["-c", "mcp_servers.t.enabled=false", "-c", 'mcp_servers.t.command="t"']
        for argv in (
            tui.execution_argv(plan, snapshot, "hooks.state={trusted}"),
            tui.trust_args(plan, snapshot),
        ):
            with self.subTest(argv=argv):
                self.assertTrue(holds(argv, off), argv)
                self.assertNotIn("mcp_servers={}", argv)

    def test_supplied_flags_env_and_hooks_share_one_path(self):
        flags = ("--add-dir", "../../added", '--config=model_verbosity="low"', "--enable", "web")
        spec = dataclasses.replace(self.spec, flags=flags, env={"S": "s"})
        launch, data = self.launch(spec)
        plan = tui.decode(data)
        snapshot = self.common.load(data["workdir"], data["sid"]).snapshot
        argv = tui.execution_argv(plan, snapshot, "hooks.state={trusted}")
        start = argv.index("--add-dir")
        self.assertEqual(argv[start : start + len(flags)], list(flags))
        self.assertEqual(launch.env["S"], "s")
        self.assertNotIn("add_dirs", data)
        self.assertEqual(
            common.added_dirs(snapshot.flags, str(self.cwd)),
            [os.path.join(self.cwd, "../../added")],
        )
        hook_flags = [
            word
            for event in self.client.tui().hooks
            for word in ("-c", next(word for word in argv if word.startswith(f"hooks.{event}=")))
        ]
        self.assertEqual(
            tui.trust_args(plan, snapshot),
            [*hook_flags, "-c", 'model_verbosity="low"', "--enable", "web"],
        )
        self.assert_no_policy(argv)

    def assert_no_policy(self, argv):
        self.assertNotIn("-s", argv)
        self.assertFalse([w for w in argv if w.startswith(POLICY)])

    def test_flags_after_separator_are_preserved_in_snapshot(self):
        flags = ("--", "resume", "-C", "/x")
        spec = dataclasses.replace(self.spec, flags=flags)
        self.launch(spec)
        self.assertEqual(self.common.load(str(self.work), spec.sid).snapshot.flags, tuple(flags))

    def test_every_grant_source_may_cover_the_hook_script(self):
        guarded = self.root / "guarded"
        cases = (
            (("--add-dir", "../../guarded"), str(guarded / "hook.py")),
            (("--add-dir", str(guarded)), str(guarded / "hook.py")),
            ((f"--add-dir={guarded / 'sub'}",), str(guarded)),
            ((), str(self.cwd / "hook.py")),
            (("--add-dir", str(self.work)), str(self.work)),
            ((), str(self.extra / "hook.py")),
        )
        for index, (flags, path) in enumerate(cases):
            spec = dataclasses.replace(self.spec, sid=f"guarded-{index}", flags=flags)
            with self.subTest(flags=flags, path=path):
                _, data = self.launch(spec, hook=(*HOOK[:2], path, *HOOK[3:]))
            self.assertEqual(data["hook"][2], path)
            self.assertIsNotNone(self.common.load(str(self.work), spec.sid))

    def test_resume_keeps_saved_grants_and_ignores_new_grants(self):
        guarded = self.root / "guarded"
        spec = dataclasses.replace(self.spec, flags=("--add-dir", str(guarded)))
        self.launch(spec)
        self.common.bind(str(self.work), spec.sid, NATIVE)
        self.rollout()
        changed = dataclasses.replace(spec, flags=())
        self.launch(changed, resume=True, hook=(*HOOK[:2], str(guarded / "hook.py"), *HOOK[3:]))
        changed = dataclasses.replace(spec, flags=("--add-dir", "/unrecorded"))
        _, data = self.launch(changed, resume=True)
        self.assertEqual(
            common.added_dirs(
                self.common.load(data["workdir"], data["sid"]).snapshot.flags, str(self.cwd)
            ),
            [str(guarded)],
        )


class LaunchPolicy(Base):
    def argv(self, client, spec, *, resume=False):
        with mock.patch("shutil.which", return_value="/protected/codex"):
            launch = committed_tui(
                client, dataclasses.replace(spec, prompt="hi", resume=resume), HOOK
            )
        return tui.execution_argv(
            tui.decode(json.loads(launch.argv[-1])),
            client.common.load(spec.workdir, spec.sid).snapshot,
            "hooks.state={trusted}",
        )

    def test_supplied_policy_and_saved_replay_in_both_modes(self):
        mode = "tui"
        for saved, current in (
            ("workspace-write", "danger-full-access"),
            ("danger-full-access", "workspace-write"),
        ):
            with self.subTest(mode=mode, saved=saved):
                flags = (
                    "-s",
                    saved,
                    "-c",
                    'approval_policy="on-request"',
                    "-c",
                    'approvals_reviewer="auto_review"',
                    "-c",
                    "sandbox_workspace_write.network_access=false",
                )
                spec = dataclasses.replace(
                    self.spec, sid=f"{mode}-{saved}", flags=flags, env={"K": "saved"}
                )
                first = self.argv(self.client, spec)
                self.assertEqual(first[first.index("-s") :][: len(flags)], list(flags))
                self.common.bind(str(self.work), spec.sid, NATIVE)
                self.rollout()
                changed = dataclasses.replace(spec, flags=("-s", current), env={"K": "changed"})
                second = self.argv(self.client, changed, resume=True)
                self.assertEqual(second[second.index("-s") :][: len(flags)], list(flags))
                self.assertEqual(
                    self.common.load(str(self.work), spec.sid).snapshot.flags, tuple(flags)
                )

    def test_separator_tail_follows_generated_options_on_fresh_and_resume(self):
        mode = "tui"
        with self.subTest(mode=mode):
            supplied = ("--enable", "web", "--", "literal", "--last")
            spec = dataclasses.replace(self.spec, sid=f"separator-{mode}", flags=supplied)
            first = self.argv(self.client, spec)
            self.common.bind(str(self.work), spec.sid, NATIVE)
            self.rollout()
            changed = dataclasses.replace(spec, flags=("--", "changed"))
            second = self.argv(self.client, changed, resume=True)
            for argv in (first, second):
                boundary = argv.index("--")
                self.assertEqual(argv[boundary:], ["--", "literal", "--last", "hi"])
                self.assertIn("gpt-test", argv[:boundary])
                self.assertIn(str(self.extra), argv[:boundary])
                self.assertIn("project_doc_max_bytes=0", argv[:boundary])
                self.assertEqual(argv[:boundary].count("hooks.state={trusted}"), 1)
                self.assertEqual(
                    sum(
                        word.startswith("hooks.")
                        for word in argv[:boundary]
                        if word != "hooks.state={trusted}"
                    ),
                    len(tui.HOOKS),
                )
            self.assertEqual(
                self.common.load(str(self.work), spec.sid).snapshot.flags, tuple(supplied)
            )


class PlanTest(PlanCase):
    def test_wire_roundtrip_decodes_an_object_without_reparsing_json(self):
        encoded = tui.encode(self.plan)
        data = json.loads(encoded)
        self.assertEqual(
            set(data),
            {
                "executable",
                "selection",
                "prompt",
                "hook",
                "workdir",
                "sid",
                "store",
                "permission",
                "flags",
            },
        )
        self.assertEqual(data["hook"], list(self.plan.hook.argv))
        with mock.patch("json.loads", side_effect=AssertionError("launch JSON decoded twice")):
            decoded = tui.decode(data)
            with self.assertRaisesRegex(ValueError, "launch fields"):
                tui.decode(encoded)
        self.assertEqual(decoded, self.plan)

    def test_decoded_plan_is_detached_from_mutable_wire_input(self):
        encoded = tui.encode(self.plan)
        data = json.loads(encoded)
        decoded = tui.decode(data)
        for key in ("selection", "hook"):
            data[key].append("changed")
        data["prompt"] = "changed"
        self.assertEqual(decoded, self.plan)
        self.assertEqual(tui.encode(decoded), encoded)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            decoded.prompt = "changed"

    def test_wire_rejects_competing_command_and_directory_authorities(self):
        data = json.loads(tui.encode(self.plan))
        for key, value in (
            ("argv", ["/foreign/codex"]),
            ("prefix", ["/foreign/codex", "--add-dir", "/foreign-write-root"]),
            ("commands", {"Stop": "foreign"}),
            ("hook_flags", ["-c", "hooks.Stop=[]"]),
            ("add_dirs", ["/foreign-write-root"]),
            ("options", ["--add-dir", "/foreign-write-root"]),
        ):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "launch fields"):
                tui.decode(data | {key: value})

    def test_wire_permission_flags_grant_no_directory_hooks_or_cwd(self):
        data = json.loads(tui.encode(self.plan))
        for value in (["--add-dir", "/foreign"], ["-C", "/foreign"], ["-c", "hooks.Stop=[]"]):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, "^Codex tui launch: "),
            ):
                tui.decode(data | {"permission": value})
        self.assertEqual(tui.decode(data | {"permission": ["--yolo"]}).permission, ("--yolo",))

    def test_wire_configured_flags_grant_no_directory_hooks_or_cwd(self):
        data = json.loads(tui.encode(self.plan))
        for value in (["--add-dir", "/foreign"], ["-C", "/foreign"], ["-c", "hooks.Stop=[]"]):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, "^Codex tui launch: "),
            ):
                tui.decode(data | {"flags": value})
        flags = ["--disable", "apps"]
        self.assertEqual(tui.decode(data | {"flags": flags}).flags, tuple(flags))

    def test_the_configs_flags_go_between_the_mode_and_the_recorded_ones(self):
        plan = dataclasses.replace(self.plan, permission=("--yolo",), flags=("--disable", "apps"))
        command = tui.execution_argv(plan, self.snapshot, "trusted")
        start = len(self.frontend)
        self.assertEqual(
            command[start : start + 5], ["--yolo", "--disable", "apps", "-m", "requested"]
        )
        self.assertEqual(
            tui.trust_args(plan, self.snapshot)[-6:],
            ["--disable", "apps", "-c", 'model_verbosity="low"', "--enable", "web"],
        )

    def test_wire_validates_each_command_component_and_prompt(self):
        data = json.loads(tui.encode(self.plan))
        for key in ("selection", "hook"):
            for value in (None, "word", [1], ["a\0b"]):
                with (
                    self.subTest(key=key, value=value),
                    self.assertRaisesRegex(ValueError, "invalid command"),
                ):
                    tui.decode(data | {key: value})
        with self.assertRaisesRegex(ValueError, "invalid command"):
            tui.decode(data | {"hook": []})
        for value in (None, 1, "relative", "/a\0b"):
            with (
                self.subTest(executable=value),
                self.assertRaisesRegex(ValueError, "absolute paths"),
            ):
                tui.decode(data | {"executable": value})
        for word in ("--add-dir", "-c", "--", "-s", "--sandbox=danger-full-access"):
            with self.subTest(selection=word), self.assertRaisesRegex(ValueError, "selection"):
                tui.decode(data | {"selection": ["resume", "saved-native", word]})
        for value in (None, 1, "a\0b"):
            with self.subTest(prompt=value), self.assertRaisesRegex(ValueError, "invalid prompt"):
                tui.decode(data | {"prompt": value})

    def test_only_the_question_tool_runs_the_pre_tool_use_hook(self):
        matched = [flag for flag in tui.hook_flags(self.plan.hook) if "matcher=" in flag]
        self.assertEqual(len(matched), 1)
        self.assertTrue(matched[0].startswith('hooks.PreToolUse=[{matcher="^request_user_input$",'))

    def test_execution_and_trust_use_the_same_pre_separator_flags(self):
        hooks = tui.hook_flags(self.plan.hook)
        trusted = "hooks.state={trusted}"
        command = tui.execution_argv(self.plan, self.snapshot, trusted)
        self.assertEqual(
            command,
            [
                *self.frontend,
                "-m",
                "requested",
                "-c",
                'model_verbosity="low"',
                "--enable",
                "web",
                "-m",
                "configured",
                "--add-dir",
                "/granted",
                *hooks,
                "-c",
                trusted,
                "--",
                "--config=literal",
                "--disable",
                "literal",
                self.plan.prompt,
            ],
        )
        self.assertEqual(
            tui.trust_args(self.plan, self.snapshot),
            [*hooks, "-c", 'model_verbosity="low"', "--enable", "web"],
        )
        changed = dataclasses.replace(self.snapshot, flags=("--disable", "web"))
        self.assertEqual(tui.trust_args(self.plan, changed), [*hooks, "--disable", "web"])
        changed_command = tui.execution_argv(self.plan, changed, trusted)
        self.assertEqual(changed_command[len(self.frontend) :][:2], ["--disable", "web"])
        self.assertNotIn("--enable", changed_command)

    def test_empty_flags_and_prompt_preserve_the_literal_tail_contract(self):
        for flags, prompt, tail in (
            ((), "", []),
            ((), "hello", ["--", "hello"]),
            (("--",), "", ["--"]),
            (("--",), "hello", ["--", "hello"]),
            (("--", "--config=literal"), "", ["--", "--config=literal"]),
            (("--", "--config=literal"), "hello", ["--", "--config=literal", "hello"]),
        ):
            plan = dataclasses.replace(self.plan, prompt=prompt)
            snapshot = dataclasses.replace(self.snapshot, flags=flags)
            with self.subTest(flags=flags, prompt=prompt):
                self.assertEqual(
                    tui.execution_argv(plan, snapshot, "trusted"),
                    [
                        *self.frontend,
                        "-m",
                        "configured",
                        "--add-dir",
                        "/granted",
                        *tui.hook_flags(plan.hook),
                        "-c",
                        "trusted",
                        *tail,
                    ],
                )
                self.assertEqual(tui.trust_args(plan, snapshot), list(tui.hook_flags(plan.hook)))

    def test_execution_write_grants_come_from_the_saved_snapshot(self):
        encoded = tui.encode(self.plan)
        command = tui.execution_argv(tui.decode(json.loads(encoded)), self.snapshot, "trusted")
        self.assertEqual(command[command.index("--add-dir") :][:2], ["--add-dir", "/granted"])
        self.assertNotIn("/foreign-write-root", command)
        changed = dataclasses.replace(self.snapshot, dirs=("/different-saved-grant",))
        changed_command = tui.execution_argv(self.plan, changed, "trusted")
        self.assertEqual(
            changed_command[changed_command.index("--add-dir") :][:2],
            ["--add-dir", "/different-saved-grant"],
        )
        self.assertNotIn("/granted", changed_command)
        self.assertEqual(tui.encode(self.plan), encoded)

    def test_standard_session_selectors_roundtrip_and_execute(self):
        for selection in ((), ("resume", "saved-native")):
            plan = dataclasses.replace(self.plan, selection=selection)
            decoded = tui.decode(json.loads(tui.encode(plan)))
            with self.subTest(selection=selection):
                self.assertEqual(decoded.selection, selection)
                command = tui.execution_argv(decoded, self.snapshot, "trusted")
                self.assertEqual(command[: 1 + len(selection)], [plan.executable, *selection])
                self.assertEqual(
                    command[1 + len(selection) :][:5],
                    ["--no-daemon", "-C", "/work", "-c", 'tui.notification_method="bel"'],
                )

    def test_fixed_frontend_uses_the_saved_snapshot_cwd(self):
        snapshot = dataclasses.replace(self.snapshot, cwd="/work/restored-project")
        command = tui.execution_argv(self.plan, snapshot, "trusted")
        self.assertEqual(command[command.index("-C") :][:2], ["-C", snapshot.cwd])
        self.assertNotIn("/work", command)


class HookTest(Base):
    def payload(self, **fields):
        return {"session_id": NATIVE, "cwd": str(self.cwd), **fields}

    def test_bind_before_forward_and_ignore_children_foreign_or_rebound(self):
        self.prepare()
        self.assertTrue(tui.bind("SessionStart", self.payload(), str(self.work), self.spec.sid))
        self.assertEqual(self.common.load(str(self.work), self.spec.sid).native_id, NATIVE)
        for fields in (
            {"agent_id": CHILD},
            {"cwd": str(self.extra)},
            {"session_id": CHILD},
            {"session_id": "invalid"},
            {"hook_event_name": "PermissionRequest"},
        ):
            with self.subTest(fields=fields), contextlib.redirect_stderr(io.StringIO()):
                self.assertFalse(
                    tui.bind("Stop", self.payload(**fields), str(self.work), self.spec.sid)
                )
        self.assertFalse(tui.bind("unknown", self.payload(), str(self.work), self.spec.sid))
        self.assertEqual(self.common.load(str(self.work), self.spec.sid).native_id, NATIVE)

    def test_callback_binds_and_adds_label_without_another_payload_reader(self):
        self.prepare()
        payload = self.payload(last_assistant_message="reply")
        state = {"store": common.SessionStore().root}

        def call(given):
            return HookCall("Stop", given, self.spec.sid, str(self.work), None, state)

        self.assertEqual(tui.on_hook(call(payload)), {**payload, "hook_event_name": "Stop"})
        self.assertEqual(self.common.load(str(self.work), self.spec.sid).native_id, NATIVE)
        self.assertIsNone(tui.on_hook(call(self.payload(agent_id=CHILD))))

    def test_invalid_identity_is_ignored_before_loading_a_record(self):
        for fields in (
            {"agent_id": CHILD},
            {"session_id": None},
            {"session_id": "invalid"},
            {"cwd": None},
            {"cwd": 1},
            {"cwd": "relative"},
            {"cwd": "/invalid\0cwd"},
            {"hook_event_name": "SessionStart"},
        ):
            with self.subTest(fields=fields), contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertFalse(
                    tui.bind("Stop", self.payload(**fields), "relative", self.spec.sid)
                )
                self.assertEqual(error.getvalue(), "")

    def test_valid_identity_reports_record_failure_before_foreign_cwd_filter(self):
        with contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertFalse(
                tui.bind("Stop", self.payload(cwd=str(self.extra)), str(self.work), self.spec.sid)
            )
        self.assertEqual(error.getvalue(), "codex hook: missing Codex recovery state\n")

    def test_cwd_alias_binds_against_the_saved_canonical_path(self):
        self.prepare()
        alias = self.root / "cwd-alias"
        alias.symlink_to(self.cwd, target_is_directory=True)
        self.assertTrue(
            tui.bind("Stop", self.payload(cwd=str(alias)), str(self.work), self.spec.sid)
        )
        self.assertEqual(self.common.load(str(self.work), self.spec.sid).native_id, NATIVE)

    def test_missing_malformed_and_fifo_record_fail_without_forwarding(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(tui.bind("Stop", self.payload(), str(self.work), self.spec.sid))
        self.prepare()
        path = self.record()
        path.write_text("not-json")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(tui.bind("Stop", self.payload(), str(self.work), self.spec.sid))
        path.unlink()
        os.mkfifo(path, 0o600)
        before = time.monotonic()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(tui.bind("Stop", self.payload(), str(self.work), self.spec.sid))
        self.assertLess(time.monotonic() - before, 0.5)

    def test_binding_lock_wait_is_bounded(self):
        self.prepare()
        with (
            mock.patch("fcntl.flock", side_effect=[BlockingIOError, None]),
            mock.patch("time.monotonic", side_effect=[0, 2]),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertFalse(tui.bind("Interrupt", self.payload(), str(self.work), self.spec.sid))
        self.assertIsNone(self.common.load(str(self.work), self.spec.sid).native_id)

    def test_explicit_caller_store_survives_configured_client_home(self):
        self.prepare()
        store = common.SessionStore().root
        client_home = self.root / "client-home"
        client_home.mkdir()
        with mock.patch.dict(os.environ, {"HOME": str(client_home)}):
            self.assertTrue(
                tui.bind("Stop", self.payload(), str(self.work), self.spec.sid, root=store)
            )
        self.assertEqual(self.common.load(str(self.work), self.spec.sid).native_id, NATIVE)
        self.assertFalse((client_home / ".ensemble").exists())

    def test_explicit_store_keeps_relative_and_implicit_root_guards(self):
        self.prepare()
        store = self.record().parent
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(
                tui.bind("Stop", self.payload(), str(self.work), self.spec.sid, root="relative")
            )
        with (
            mock.patch.dict(os.environ, {"TMPDIR": str(store.parent)}),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertFalse(
                tui.bind("Stop", self.payload(), str(self.work), self.spec.sid, root=str(store))
            )
        self.assertIsNone(self.common.load(str(self.work), self.spec.sid).native_id)

    def test_authoritative_store_validates_every_recovery_path_with_writable_client_home(self):
        state = self.prepare()
        record = self.record()
        store = str(record.parent)
        client_home = self.cwd / "client-home"
        client_home.mkdir()
        with mock.patch.dict(os.environ, {"HOME": str(client_home)}):
            self.assertEqual(
                common.SessionStore(store).lookup(self.spec.sid, state.snapshot.cwd), state
            )
            self.assertEqual(common.SessionStore(store).load(str(self.work), self.spec.sid), state)
            bound = common.SessionStore(store).bind(str(self.work), self.spec.sid, NATIVE)
            self.assertEqual(bound.native_id, NATIVE)
            self.assertTrue(
                tui.bind("Stop", self.payload(), str(self.work), self.spec.sid, root=store)
            )
            fresh = common.SessionStore(store).create(
                str(self.work),
                "explicit-store",
                state.snapshot,
                state.writable_roots,
            )
            self.assertEqual(common.SessionStore(store).save(fresh), fresh)
            self.assertEqual(common.SessionStore(store).load(str(self.work), fresh.sid), fresh)
        self.assertEqual(self.common.load(str(self.work), self.spec.sid).native_id, NATIVE)
        self.assertFalse((client_home / ".ensemble").exists())

    def test_authoritative_store_still_rejects_actual_writable_store(self):
        state = self.prepare()
        record = self.record()
        unsafe = self.cwd / ".private-state" / "sessions" / "codex"
        for directory in (unsafe.parent.parent, unsafe.parent, unsafe):
            directory.mkdir(mode=0o700, exist_ok=True)
        copied = unsafe / record.name
        copied.write_bytes(record.read_bytes())
        copied.chmod(0o600)
        root = str(unsafe)
        calls = (
            lambda: common.SessionStore(root).lookup(self.spec.sid, state.snapshot.cwd),
            lambda: common.SessionStore(root).load(str(self.work), self.spec.sid),
            lambda: common.SessionStore(root).bind(str(self.work), self.spec.sid, NATIVE),
            lambda: common.SessionStore(root).create(
                str(self.work), "unsafe-store", state.snapshot, state.writable_roots
            ),
            lambda: common.SessionStore(root).save(state),
        )
        for call in calls:
            with (
                self.subTest(call=call),
                self.assertRaisesRegex(common.RecoveryError, "inside writable root"),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                call()
        self.assertEqual(record.read_bytes(), copied.read_bytes())
        self.assertEqual(list(unsafe.iterdir()), [copied])
        self.assertIsNone(self.common.load(str(self.work), self.spec.sid).native_id)
