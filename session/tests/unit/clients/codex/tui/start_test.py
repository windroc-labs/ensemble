"""Codex startup package bootstrap, trust, launch validation and process cleanup."""

import contextlib
import copy
import dataclasses
import io
import json
import os
import pathlib
import shlex
import sys
from unittest import mock

from ensemble.session.clients.codex import tui
from ensemble.session.clients.codex.tui import start as tui_start
from ensemble.session.engine.types import ClientContext, Container, Permission
from ensemble.utils import LOCAL_ENV_VARS

from session.tests.case import Case
from session.tests.codex_tui import HOOK, ROOTS_JSON, LaunchCase
from session.tests.source_roots import PACKAGE_ROOTS
from session.tests.unit.clients.codex.support import NATIVE, Base, committed_tui

DIRECTORY = pathlib.Path(tui_start.__file__).parent


class DecodeLaunchDataTest(Case):
    def test_lone_surrogate_is_a_start_error_before_decoding(self):
        valid = json.dumps({"prompt": "\ud800"}, ensure_ascii=False)
        for argv in ("\ud800", valid):
            with (
                self.subTest(argv=argv[:20]),
                mock.patch("json.loads") as decode,
                self.assertRaisesRegex(tui_start.StartError, "surrogates not allowed"),
            ):
                tui_start._decode_launch_data([argv])
            decode.assert_not_called()

    def test_main_reports_launch_validation_as_one_line_and_failure_status(self):
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            self.assertEqual(tui_start.main({}), 1)
        self.assertEqual(stderr.getvalue(), "codex tui: Codex tui launch: invalid launch fields\n")


class StartTest(Base):
    def catalog(self):
        commands = {event: "command-" + event for event, name, _ in tui_start.HOOKS}
        hooks = [
            {
                "source": "sessionFlags",
                "eventName": name,
                "enabled": True,
                "handlerType": "command",
                "command": commands[event],
                "key": "key-" + name,
                "currentHash": "sha256:" + "a" * 64,
            }
            for event, name, _ in tui_start.HOOKS
        ]
        return {"data": [{"hooks": hooks, "errors": []}]}, commands

    def test_trust_exact_enabled_commands_and_hashes_only(self):
        result, commands = self.catalog()
        state = tui_start.trusted(result, str(self.cwd), commands)
        self.assertTrue(state.startswith("hooks.state={"))
        self.assertIn('"key-stop"={trusted_hash="sha256:', state)
        for field, value in (
            ("enabled", False),
            ("command", "foreign"),
            ("currentHash", "bad"),
            ("key", None),
        ):
            changed = copy.deepcopy(result)
            changed["data"][0]["hooks"][0][field] = value
            with self.subTest(field=field), self.assertRaises(tui_start.StartError):
                tui_start.trusted(changed, str(self.cwd), commands)
        for change in ({"data": []}, {"data": [{"hooks": []}]}, {"data": [{"hooks": None}]}):
            with self.assertRaises(tui_start.StartError):
                tui_start.trusted(change, str(self.cwd), commands)
        result["data"][0]["hooks"].append(
            {"source": "user", "enabled": True, "trustStatus": "modified", "key": "user"}
        )
        with self.assertRaisesRegex(tui_start.StartError, "review"):
            tui_start.trusted(result, str(self.cwd), commands)

    def test_opaque_hook_words_survive_launch_and_exact_trust_matching(self):
        for index, hook in enumerate(
            (
                (*HOOK, "extra opaque word"),
                (*HOOK[:-1], json.dumps(PACKAGE_ROOTS, indent=2)),
                (*HOOK[:-1], "not JSON at all"),
            )
        ):
            spec = dataclasses.replace(self.spec, sid=f"opaque-trust-{index}")
            with (
                self.subTest(hook=hook),
                mock.patch("shutil.which", return_value="/protected/codex"),
            ):
                launch = committed_tui(
                    self.client, dataclasses.replace(spec, prompt="prompt"), hook
                )
                plan = tui.decode(json.loads(launch.argv[-1]))
                catalog, _ = self.catalog()
                rows = catalog["data"][0]["hooks"]
                for row, (event, _, _) in zip(rows, tui_start.HOOKS, strict=True):
                    row["command"] = shlex.join((*hook, event))
                commands = tui.commands(plan.hook)
                trusted = tui_start.trusted(catalog, str(self.cwd), commands)
                self.assertIn('"key-stop"={trusted_hash="sha256:', trusted)
                rows[0]["command"] = shlex.join((*hook[:-1], "SessionStart"))
                with self.assertRaises(tui_start.StartError):
                    tui_start.trusted(catalog, str(self.cwd), commands)

    def test_start_adds_transient_trust_and_execs_saved_home_without_config_write(self):
        run = self.client.tui()
        hook = HOOK
        with mock.patch("shutil.which", return_value="/protected/codex"):
            launch = run.launch(dataclasses.replace(self.spec, prompt="prompt"), hook)
        run.commit()
        before = {str(p): p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        with (
            contextlib.nullcontext(mock.Mock(return_value="hooks.state={exact}")) as trust,
            mock.patch("os.chdir") as chdir,
            mock.patch("os.execve") as execute,
        ):
            tui_start.launch(json.loads(launch.argv[-1]), trust=trust)
        chdir.assert_called_once_with(str(self.cwd))
        exe, argv, env = execute.call_args.args
        self.assertEqual(exe, "/protected/codex")
        self.assertEqual(argv[-4:], ["-c", "hooks.state={exact}", "--", "prompt"])
        self.assertEqual(env["CODEX_HOME"], str(self.codex_home))
        self.assertEqual(trust.call_args.kwargs["cwd"], str(self.cwd))
        self.assertEqual(
            before, {str(p): p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        )

    def test_projected_resume_reader_preserves_native_identity_and_host_record(self):
        self.prepare()
        self.rollout()
        assert self.spec.sid is not None
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        record = self.record()
        before = record.read_bytes()
        runtime = ClientContext(
            "host.docker.internal",
            Permission.AUTO,
            Container(
                "/opt/ensemble/bin", "/image/python", "/image/modules", str(self.root / "volatile")
            ),
        )
        run = self.client.tui()
        run._context = runtime
        with mock.patch("shutil.which", side_effect=AssertionError("host lookup")):
            launch = run.launch(dataclasses.replace(self.spec, resume=True), HOOK)
        run.commit()
        self.assertEqual(len(run._runtime_files), 1)
        for path, content in run._runtime_files.items():
            projected = pathlib.Path(path)
            projected.parent.mkdir(parents=True, mode=0o700)
            projected.write_text(content)
            self.assertEqual(json.loads(content)["native_id"], NATIVE)
        trust = mock.Mock(return_value="hooks.state={exact}")
        with (
            mock.patch.dict(os.environ, launch.env),
            mock.patch("os.chdir"),
            mock.patch("os.execve") as execute,
        ):
            tui_start.launch(json.loads(launch.argv[-1]), trust=trust)
        exe, argv, env = execute.call_args.args
        self.assertEqual((exe, argv[:3]), ("/opt/ensemble/bin/codex", [exe, "resume", NATIVE]))
        self.assertEqual(env["CODEX_HOME"], str(self.codex_home))
        self.assertEqual(trust.call_args.kwargs["root"], str(self.root / "volatile/codex-store"))
        self.assertEqual(record.read_bytes(), before)
        state = self.common.load(str(self.work), self.spec.sid)
        assert state is not None
        self.assertEqual(state.native_id, NATIVE)

    def test_start_passes_session_env_and_clears_git_local_environment(self):
        access = dataclasses.replace(self.spec.access, env={"PYTHONPATH": "/a b;c"})
        spec = dataclasses.replace(self.spec, access=access)
        with mock.patch("shutil.which", return_value="/protected/codex"):
            launch = committed_tui(
                self.client,
                dataclasses.replace(spec, prompt="prompt"),
                (sys.executable, "-I", "/protected/hook.py", "/protected/session", ROOTS_JSON),
            )
        with (
            mock.patch.dict(
                os.environ, {**launch.env, **dict.fromkeys(LOCAL_ENV_VARS, "inherited")}
            ),
            contextlib.nullcontext(mock.Mock(return_value="hooks.state={exact}")) as trust,
            mock.patch("os.chdir"),
            mock.patch("os.execve") as execute,
        ):
            tui_start.launch(json.loads(launch.argv[-1]), trust=trust)
        self.assertEqual(execute.call_args.args[2]["PYTHONPATH"], "/a b;c")
        self.assertFalse(LOCAL_ENV_VARS.intersection(execute.call_args.args[2]))
        self.assertFalse(LOCAL_ENV_VARS.intersection(trust.call_args.kwargs["env"]))

    def test_the_client_gets_its_own_pythonpath_not_the_start_processes(self):
        with mock.patch("shutil.which", return_value="/protected/codex"):
            launch = committed_tui(
                self.client,
                dataclasses.replace(self.spec, prompt="prompt"),
                (sys.executable, "-I", "/protected/hook.py", "/protected/session", ROOTS_JSON),
            )
        data = json.loads(launch.argv[-1])
        own = {k: v for k, v in launch.env.items() if k != tui.CLIENT_PYTHONPATH}
        for given in (None, "/client lib"):
            client = {} if given is None else {tui.CLIENT_PYTHONPATH: given}
            with (
                self.subTest(given=given),
                mock.patch.dict(os.environ, {**own, **client}),
                contextlib.nullcontext(mock.Mock(return_value="hooks.state={exact}")) as trust,
                mock.patch("os.chdir"),
                mock.patch("os.execve") as execute,
            ):
                tui_start.launch(data, trust=trust)
            for env in (execute.call_args.args[2], trust.call_args.kwargs["env"]):
                self.assertEqual(env.get("PYTHONPATH"), given)
                self.assertNotIn(tui.CLIENT_PYTHONPATH, env)

    def test_hooks_list_initializes_and_closes_mock_process(self):
        proc = mock.Mock()
        proc.stdout.fileno.return_value = 42
        with (
            mock.patch("subprocess.Popen", return_value=proc) as popen,
            mock.patch("select.select", return_value=([42], [], [])),
            mock.patch(
                "os.read",
                side_effect=[
                    b'{"id":1,"result":{}}\n{"id":2,"result":{"data":[]}}\n',
                    b" ".strip(),
                ],
            ),
            mock.patch("os.killpg"),
        ):
            self.assertEqual(
                tui_start.hooks_list("/protected/fake", ["-c", "hook"], str(self.cwd), {}),
                {"data": []},
            )
        self.assertEqual(popen.call_args.args[0], ["/protected/fake", "app-server", "-c", "hook"])
        sent = [json.loads(call.args[0]) for call in proc.stdin.write.call_args_list]
        self.assertEqual(
            [msg["method"] for msg in sent], ["initialize", "initialized", "hooks/list"]
        )
        self.assertEqual(sent[-1]["params"], {"cwds": [str(self.cwd)]})
        proc.stdin.close.assert_called_once_with()
        proc.stdout.close.assert_called_once_with()

    def test_retry_once_after_appserver_exit_and_start_lock_timeout(self):
        self.prepare()
        result, commands = self.catalog()
        with (
            contextlib.nullcontext(
                mock.Mock(side_effect=[tui_start.app_server.Exited(1), result])
            ) as listing,
            mock.patch("time.sleep"),
        ):
            self.assertTrue(
                tui_start.hook_trust(
                    exe="fake",
                    args=[],
                    cwd=str(self.cwd),
                    env={"CODEX_HOME": str(self.codex_home)},
                    listing=listing,
                    commands=commands,
                ).startswith("hooks.state=")
            )
        self.assertEqual(listing.call_count, 2)
        with (
            mock.patch("fcntl.flock", side_effect=BlockingIOError),
            mock.patch("time.monotonic", side_effect=[0, 31]),
            self.assertRaisesRegex(tui_start.StartError, "held"),
        ):
            tui_start.hook_trust(
                exe="fake",
                args=[],
                cwd=str(self.cwd),
                env={"CODEX_HOME": str(self.codex_home)},
                commands=commands,
            )

    def test_missing_duplicate_foreign_hook_rows_and_disabled_user_hooks(self):
        result, commands = self.catalog()
        rows = result["data"][0]["hooks"]
        for changed in (
            rows[1:],
            [*rows, rows[0]],
            [*rows, {"source": "sessionFlags", "eventName": "foreign"}],
        ):
            with self.assertRaises(tui_start.StartError):
                tui_start.trusted({"data": [{"hooks": changed}]}, str(self.cwd), commands)
        for extra in (
            {"source": "user", "enabled": False, "trustStatus": "modified"},
            {"source": "user", "enabled": True, "isManaged": True, "trustStatus": "untrusted"},
            {"source": "user", "enabled": True, "trustStatus": "trusted"},
        ):
            self.assertTrue(
                tui_start.trusted(
                    {"data": [{"hooks": [*rows, extra]}]}, str(self.cwd), commands
                ).startswith("hooks.state=")
            )

    def test_an_unreadable_mcp_layer_fails_the_start_in_one_line_before_any_spawn(self):
        run = self.client.tui()
        spec = dataclasses.replace(self.spec, prompt="prompt", flags=("-c", "mcp_servers={}"))
        with mock.patch("shutil.which", return_value="/protected/codex"):
            launch = run.launch(spec, HOOK)
        run.commit()
        (self.codex_home / "config.toml").write_text("[mcp_servers\n")
        stderr = io.StringIO()
        with mock.patch("subprocess.Popen") as proc, mock.patch("sys.stderr", stderr):
            self.assertEqual(tui_start.main(json.loads(launch.argv[-1])), 1)
        proc.assert_not_called()
        self.assertRegex(stderr.getvalue(), r"^codex tui: .*config\.toml: invalid TOML: .*\n$")

    def test_bad_launch_data_does_not_spawn(self):
        with mock.patch("subprocess.Popen") as proc:
            for data in ("null", "not-json", "{}"):
                with self.assertRaises((tui_start.StartError, ValueError)):
                    tui_start.launch(tui_start._decode_launch_data([data]))
        proc.assert_not_called()

    def test_signal_during_spawn_cleans_owned_process_without_retry_or_exec(self):
        run = self.client.tui()
        with mock.patch("shutil.which", return_value="/protected/codex"):
            launch = run.launch(
                dataclasses.replace(self.spec, prompt="prompt"),
                (sys.executable, "-I", "/protected/hook.py", "/protected/session", ROOTS_JSON),
            )
        run.commit()
        for signum in (tui_start.signal.SIGHUP, tui_start.signal.SIGTERM):
            with self.subTest(signum=signum):
                proc = mock.Mock(pid=123)
                proc.stdout.fileno.return_value = 42
                proc.wait.return_value = 0
                original = tui_start.signal.getsignal(signum)

                def spawn(*_args, proc=proc, signum=signum, **_kwargs):
                    tui_start.signal.raise_signal(signum)
                    return proc

                with (
                    mock.patch("subprocess.Popen", side_effect=spawn) as popen,
                    mock.patch("os.killpg") as kill,
                    mock.patch("os.execve") as execute,
                    self.assertRaises(tui_start.Stopped),
                ):
                    tui_start.launch(json.loads(launch.argv[-1]))
                self.assertEqual(popen.call_count, 1)
                kill.assert_called_with(123, tui_start.signal.SIGKILL)
                proc.wait.assert_called()
                proc.stdout.close.assert_called_once()
                execute.assert_not_called()
                self.assertEqual(tui_start.signal.getsignal(signum), original)

    def test_wrapper_starts_under_a_tmpdir_over_its_dependencies(self):
        with mock.patch("shutil.which", return_value="/protected/codex"):
            launch = committed_tui(
                self.client,
                dataclasses.replace(self.spec, prompt="hi"),
                (sys.executable, "-I", "/protected/hook.py", "/protected/session", ROOTS_JSON),
            )
        with (
            mock.patch.dict("os.environ", {"TMPDIR": "/protected"}),
            mock.patch("os.chdir"),
            mock.patch("os.execve") as execute,
        ):
            tui_start.launch(
                json.loads(launch.argv[-1]), trust=mock.Mock(return_value="hooks.state={x}")
            )
        self.assertEqual(execute.call_args.args[1][0], "/protected/codex")
        self.assertEqual(execute.call_args.args[2]["TMPDIR"], "/protected")


class LaunchDataTest(LaunchCase):
    def test_launch_data_with_a_protect_key_does_not_spawn(self):
        data = json.loads(self.launch(self.spec))
        with (
            mock.patch("subprocess.Popen") as proc,
            self.assertRaisesRegex(ValueError, "launch fields"),
        ):
            tui_start.launch({**data, "protect": []})
        proc.assert_not_called()

    def test_a_plain_start_lists_hooks_with_the_users_config_and_execs_the_flags(self):
        flags = ("-c", 'model_verbosity="low"', "--disable", "web", "-m", "o3")
        spec = dataclasses.replace(self.spec, flags=flags)
        data = self.launch(spec)
        with (
            contextlib.nullcontext(mock.Mock(return_value="hooks.state={exact}")) as trust,
            mock.patch("os.chdir"),
            mock.patch("os.execve") as execute,
        ):
            tui_start.launch(json.loads(data), trust=trust)
        argv = execute.call_args.args[1]
        start = argv.index("-c", 6)
        self.assertEqual(argv[start : start + len(flags)], list(flags))
        hook_flags = tui.hook_flags(tui.decode(json.loads(data)).hook)
        self.assertEqual(
            trust.call_args.kwargs["args"],
            [*hook_flags, "-c", 'model_verbosity="low"', "--disable", "web"],
        )
