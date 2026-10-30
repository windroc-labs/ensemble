"""The manager entry owns configuration passed across the session command boundary."""

import json
import os
import sys
from types import SimpleNamespace
from unittest import mock

from ensemble.manager import codex
from tests.manager.case import REPO, Case


class Executed(BaseException):
    pass


class LaunchTest(Case):
    def launch(self, environ, argv=(), *, context=None):
        with (
            mock.patch.dict(os.environ, {"HOME": self.home, **environ}, clear=True),
            mock.patch(
                "subprocess.run",
                return_value=context or SimpleNamespace(returncode=0, stdout='{"holder_pid":41}'),
            ) as run,
            mock.patch("os.execv", side_effect=Executed) as execute,
            self.assertRaises(Executed),
        ):
            codex.main(REPO, argv)
        executable, command = execute.call_args.args
        self.assertEqual(executable, sys.executable)
        self.assertEqual(
            command[:3],
            (sys.executable, "-I", f"{REPO}/src/ensemble/session/clients/codex/manager.py"),
        )
        return json.loads(command[3]), command[4:], run.call_args_list

    def test_agent_and_non_manager_launches_do_not_query_or_bind(self):
        argv = ("--model", "a b", "a'b$c", "--", "exec")
        for environ in ({}, {"ENSEMBLE_MANAGER": "m", "ENSEMBLE_AGENT_SESSION": "1"}):
            with self.subTest(environ=environ):
                config, actual, calls = self.launch(environ, argv)
                self.assertEqual(actual, argv)
                self.assertEqual(calls, [])
                self.assertEqual(set(config), {"excluded"})

    def test_manager_supplies_commands_context_and_only_holder_environment(self):
        environ = {
            "ENSEMBLE_MANAGER": "second",
            "ENSEMBLE_HOLDER_PID": "41",
            "ENSEMBLE_HOLDER_ID": "lease",
            "ENSEMBLE_HOLDER_GENERATION": "2",
            "SECRET": "hidden",
        }
        config, argv, calls = self.launch(environ, ("resume", "native"))
        recorder = (sys.executable, "-I", f"{REPO}/src/ensemble/manager/rearm.py")
        self.assertEqual(calls[0].args[0], (*recorder, "--binding", "context"))
        self.assertEqual(config["ancestor"], 41)
        self.assertEqual(
            config["environ"], {key: value for key, value in environ.items() if key != "SECRET"}
        )
        self.assertFalse(config["local"])
        self.assertEqual(argv, ("resume", "native"))
        for key, action in (("recorder", "record"), ("launch_recorder", "launch")):
            self.assertEqual(
                config[key], [*recorder, "--binding", action, "--client", "codex", "--client-pid"]
            )

    def test_native_environment_choices_are_owned_by_the_entry(self):
        for name in ("CODEX_EXEC_SERVER_URL", "OPENAI_WORKLOAD_IDENTITY_CONTEXT"):
            with self.subTest(name=name):
                config, _, _ = self.launch({"ENSEMBLE_MANAGER": "m", name: ""})
                self.assertTrue(config["local"])
                self.assertNotIn(name, config["environ"])

    def test_invalid_hold_passes_through_without_binding(self):
        for context in (
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=2, stdout="error"),
        ):
            with self.subTest(context=context):
                config, argv, calls = self.launch(
                    {"ENSEMBLE_MANAGER": "m"}, ("hello",), context=context
                )
                self.assertEqual(set(config), {"excluded"})
                self.assertEqual(argv, ("hello",))
                self.assertEqual(len(calls), 1)

    def test_launcher_exclusions_and_arguments_survive_the_command_boundary(self):
        config, argv, _ = self.launch({}, ("--ensemble-launcher", "/copied/codex", "--", "literal"))
        self.assertEqual(
            config["excluded"],
            [
                f"{REPO}/skills/manage/scripts/codex",
                f"{self.home}/.ensemble/bin/codex",
                "/copied/codex",
            ],
        )
        self.assertEqual(argv, ("--", "literal"))

    def test_context_failure_prevents_launch(self):
        for result in ("{", "{}"):
            with (
                self.subTest(result=result),
                mock.patch.dict(os.environ, {"ENSEMBLE_MANAGER": "m"}, clear=True),
                mock.patch(
                    "subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=result)
                ),
                mock.patch("os.execv") as execute,
                self.assertRaises((ValueError, KeyError)),
            ):
                codex.main(REPO, ())
            execute.assert_not_called()
        with (
            mock.patch.dict(os.environ, {"ENSEMBLE_MANAGER": "m"}, clear=True),
            mock.patch("subprocess.run", side_effect=codex.subprocess.TimeoutExpired("context", 5)),
            mock.patch("os.execv") as execute,
            self.assertRaises(codex.subprocess.TimeoutExpired),
        ):
            codex.main(REPO, ())
        execute.assert_not_called()
