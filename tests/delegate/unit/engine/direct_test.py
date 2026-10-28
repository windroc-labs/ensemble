"""Named environments open through the ordinary session worker CLI, with no role run."""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ensemble.delegate.engine import config, direct
from ensemble.delegate.engine.types import ConfigError
from ensemble.session import EnvironmentSpec
from tests.delegate.case import Case


class Direct(Case):
    def setUp(self):
        self.cfg = {
            "client": "claude",
            "hosts": {
                "default": "docker",
                "docker": {"implementation": config.DOCKER_HOST},
                "tmux": {"implementation": "ensemble.session.hosts.tmux:TmuxHost"},
            },
            "environment": {
                "docker": {
                    "image": EnvironmentSpec.WORKER,
                    "gateway": "workers",
                    "prefetch": False,
                }
            },
            "environments": {
                "dummy": {
                    "image": "dummy-env:test",
                    "lock": "dummy-lock",
                    "plugins": ["dummy-plugin"],
                    "credentials": {
                        "env": [{"item": "ensemble-broker-env/dummy", "file": "dummy.txt"}]
                    },
                }
            },
            "roles": {"engineer": {"environment": {"name": "dummy", "gateway": "ignored"}}},
        }
        self.started = self.enterContext(
            mock.patch("subprocess.run", return_value=SimpleNamespace(returncode=0))
        )

    def open(self, **options):
        def literal(value):
            if isinstance(value, dict):
                return (
                    "{"
                    + ", ".join(f"{json.dumps(k)} = {literal(v)}" for k, v in value.items())
                    + "}"
                )
            if isinstance(value, list):
                return "[" + ", ".join(map(literal, value)) + "]"
            return json.dumps(value)

        def table(name, values):
            scalars = "\n".join(
                f"{json.dumps(k)} = {literal(v)}"
                for k, v in values.items()
                if not isinstance(v, dict)
            )
            nested = "\n".join(
                table(f"{name}.{json.dumps(k)}", v)
                for k, v in values.items()
                if isinstance(v, dict)
            )
            return f"[{name}]\n{scalars}\n{nested}\n"

        local = table("session", {"hosts": self.cfg["hosts"]}) + table(
            "delegate", {k: v for k, v in self.cfg.items() if k != "hosts"}
        )
        Path(self.ensemble, "config.local.toml").write_text(local)
        return direct.open_worker("dummy-1", "dummy", **options)

    def argv(self):
        return self.started.call_args.args[0]

    def spec(self):
        argv = self.argv()
        return json.loads(argv[argv.index("--environment-spec") + 1])

    def test_named_environment_overlays_host_default_without_a_role(self):
        self.assertEqual(self.open(cwd=self.home), 0)
        argv = self.argv()
        self.assertEqual(
            argv[:7], [sys.executable, "-P", "-s", "-m", "ensemble.session.cli", "start", "dummy-1"]
        )
        self.assertEqual(
            self.spec(),
            {
                "image": "dummy-env:test",
                "gateway": "workers",
                "lock": "dummy-lock",
                "plugins": ["dummy-plugin"],
                "prefetch": None,
            },
        )
        self.assertEqual(
            json.loads(argv[argv.index("--environment-credentials") + 1]),
            {"env": [{"item": "ensemble-broker-env/dummy", "file": "dummy.txt"}]},
        )
        self.assertFalse({"--prompt", "--role", "--task", "--input", "--out"} & set(argv))
        self.assertEqual(argv[argv.index("--cwd") + 1], self.home)
        self.assertNotIn("shell", self.started.call_args.kwargs)
        self.assertNotIn("capture_output", self.started.call_args.kwargs)

    def test_local_default_changes_flow_through_and_lists_replace(self):
        self.cfg["environment"]["docker"].update(gateway="another", plugins=["default-plugin"])
        self.open()
        self.assertEqual(
            (self.spec()["gateway"], self.spec()["plugins"]), ("another", ["dummy-plugin"])
        )

    def test_a_named_default_equivalent_uses_the_ordinary_worker(self):
        self.cfg["environments"]["dummy"] = {"image": EnvironmentSpec.WORKER}
        self.open()
        self.assertNotIn("--environment-spec", self.argv())

    def test_a_docker_host_alias_uses_its_own_default(self):
        self.cfg["hosts"]["default"] = "d2"
        self.cfg["hosts"]["d2"] = {"implementation": config.DOCKER_HOST}
        self.cfg["environment"]["d2"] = {"image": EnvironmentSpec.WORKER, "gateway": "other"}
        self.open()
        self.assertEqual(self.spec()["gateway"], "other")
        self.assertEqual(self.argv()[self.argv().index("--host") + 1], "d2")

    def test_unknown_environment_missing_default_non_docker_and_prefetch_refuse_before_start(self):
        for change, word in (
            (lambda: self.cfg["environments"].clear(), "dummy"),
            (
                lambda: self.cfg["hosts"].update(
                    default="d2", d2={"implementation": config.DOCKER_HOST}
                ),
                "environment.d2",
            ),
            (lambda: self.cfg["hosts"].update(default="tmux"), "Docker"),
            (lambda: self.cfg["environments"]["dummy"].update(prefetch=True), "prefetch"),
            (lambda: self.cfg["environment"]["docker"].update(prefetch=True), "prefetch"),
        ):
            saved = json.loads(json.dumps(self.cfg))
            with self.subTest(word=word):
                change()
                with self.assertRaisesRegex((ConfigError, ValueError), word):
                    self.open()
                self.started.assert_not_called()
            self.cfg.clear()
            self.cfg.update(saved)

    def test_client_account_display_and_captured_identity_are_forwarded(self):
        self.open(
            client="codex",
            account="work",
            display="none",
            sid="captured",
            events="/captured/events",
        )
        argv = self.argv()
        self.assertEqual(argv[argv.index("--client") + 1], "codex")
        self.assertIn("--account=work", argv)
        self.assertIn("--display=none", argv)
        env = self.started.call_args.kwargs["env"]
        self.assertEqual(
            (env["ENSEMBLE_SID"], env["ENSEMBLE_EVENTS"]), ("captured", "/captured/events")
        )

    def test_leading_dash_display_remains_one_option_value(self):
        self.open(display="-pane")
        self.assertIn("--display=-pane", self.argv())

    def test_absent_identity_does_not_reuse_the_callers_environment(self):
        with mock.patch.dict(os.environ, {"ENSEMBLE_SID": "stale", "ENSEMBLE_EVENTS": "/stale"}):
            self.open()
        env = self.started.call_args.kwargs["env"]
        self.assertFalse({"ENSEMBLE_SID", "ENSEMBLE_EVENTS"} & env.keys())
        argv = self.argv()
        self.assertEqual(argv[argv.index("--client") + 1], "claude")
        self.assertEqual(argv[argv.index("--cwd") + 1], os.getcwd())

    def test_session_exit_codes_are_preserved_and_signals_are_failures(self):
        for code, want in ((0, 0), (1, 1), (2, 2), (75, 75), (-15, 1)):
            with self.subTest(code=code):
                self.started.return_value = SimpleNamespace(returncode=code)
                self.assertEqual(self.open(), want)

    def test_spawn_failure_is_a_runtime_error(self):
        self.started.side_effect = OSError("cannot launch session")
        with self.assertRaisesRegex(OSError, "cannot launch session"):
            self.open()
