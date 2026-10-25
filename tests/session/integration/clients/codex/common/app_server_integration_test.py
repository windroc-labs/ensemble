"""Codex account probes run the app-server client by path against a fake native server."""

import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ensemble.session.clients.codex import CodexClient
from tests.session.case import REPO, SRC, Case
from tests.session.integration.clients.codex import fake_app_server


class AppServerIntegrationTest(Case):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=os.environ.get("ENSEMBLE_TEST_TMP", REPO))).resolve()
        self.home, self.work, self.bin = (self.root / name for name in ("home", "work", "bin"))
        self.codex_home = self.home / ".codex"
        for path in (self.home, self.work, self.bin, self.codex_home):
            path.mkdir()
        self.executable = self.bin / "codex"
        self.executable.write_text(
            "#!/bin/sh\nexec "
            + shlex.quote(sys.executable)
            + " "
            + shlex.quote(fake_app_server.__file__)
            + ' "$@"\n'
        )
        self.executable.chmod(0o755)
        self.log = self.root / "probe.jsonl"
        self.config = self.codex_home / "config.toml"
        self.auth = self.codex_home / "auth.json"
        self.config.write_text('model = "preserve-config"\n')
        self.auth.write_text('{"token": "fixture-only"}\n')
        self.original = {
            path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (self.config, self.auth)
        }
        self.env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        self.env.update(
            HOME=str(self.home),
            CODEX_HOME=str(self.codex_home),
            PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
            FAKE_LOG=str(self.log),
        )
        self.script = (
            Path(SRC["session"])
            / "ensemble"
            / "session"
            / "clients"
            / "codex"
            / "common"
            / "app_server.py"
        )
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for path in (self.log, self.executable, self.config, self.auth):
            path.unlink(missing_ok=True)
        for path in (self.codex_home, self.home, self.work, self.bin, self.root):
            path.rmdir()

    def logged(self):
        return (
            [json.loads(line) for line in self.log.read_text().splitlines()]
            if self.log.exists()
            else []
        )

    def probe(self, case="normal", timeout=2, **env):
        self.log.unlink(missing_ok=True)
        started = time.monotonic()
        result = subprocess.run(
            (sys.executable, "-I", str(self.script), "--timeout", str(timeout)),
            cwd=self.work,
            env={**self.env, "FAKE_CASE": case, **env},
            capture_output=True,
            text=True,
            timeout=3,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertLess(time.monotonic() - started, timeout + 2)
        rows = self.logged()
        if rows:
            with self.assertRaises(ProcessLookupError):
                os.kill(rows[0]["pid"], 0)
        for path, original in self.original.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), original)
        self.assertEqual(set(self.codex_home.iterdir()), {self.config, self.auth})
        return json.loads(result.stdout)

    def test_standalone_executable_returns_one_raw_native_result(self):
        result = self.probe()
        weekly = {"usedPercent": 41, "windowDurationMins": 10080, "resetsAt": 4000}
        five = {"usedPercent": 62, "windowDurationMins": 300, "resetsAt": 2000}
        self.assertEqual(
            result,
            {
                "rateLimits": {"primary": None, "secondary": None},
                "rateLimitsByLimitId": {"codex": {"primary": weekly, "secondary": five}},
            },
        )
        self.assertTrue(os.path.isabs(sys.executable))
        self.assertTrue(self.script.is_absolute())
        rows = self.logged()
        self.assertEqual(rows[0]["argv"], ["app-server"])
        self.assertIsNone(rows[0]["env"]["PYTHONPATH"])
        self.assertEqual(
            rows[1:],
            [
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {"name": "ensemble", "version": "0"},
                        "capabilities": {"experimentalApi": True},
                    },
                },
                {"method": "initialized"},
                {"id": 2, "method": "account/rateLimits/read"},
            ],
        )

    def test_production_probe_isolates_stdlib_from_a_select_sibling(self):
        launch = CodexClient({}).usage_probe()
        self.assertEqual(Path(launch.argv[-1]), self.script)
        self.assertEqual(launch.argv[:2], (os.path.realpath(sys.executable), "-I"))
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            folder = Path(directory)
            copied_script = folder / "app_server.py"
            copied_script.write_bytes(self.script.read_bytes())
            marker = folder / "shadowed"
            (folder / "select.py").write_text(
                f"open({str(marker)!r}, 'w').close()\nraise RuntimeError('shadowed stdlib select')\n"
            )
            result = subprocess.run(
                (*launch.argv[:2], str(copied_script), "--timeout", "0"),
                cwd=folder,
                env=self.env,
                capture_output=True,
                text=True,
                timeout=3,
            )
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "null\n", ""))
            self.assertFalse(marker.exists())
            unisolated = subprocess.run(
                (launch.argv[0], str(copied_script), "--timeout", "0"),
                cwd=folder,
                env=self.env,
                capture_output=True,
                text=True,
                timeout=3,
            )
            self.assertNotEqual(unisolated.returncode, 0)
            self.assertEqual(unisolated.stdout, "")
            self.assertIn("shadowed stdlib select", unisolated.stderr)
            self.assertTrue(marker.exists())

    def test_partial_null_and_empty_native_snapshots_are_preserved(self):
        for case in ("weekly-only", "null", "empty", "fallback", "five-first", "weekly-full"):
            with self.subTest(case=case):
                result = self.probe(case)
                if case == "empty":
                    self.assertEqual(result, {})
                    continue
                snapshot = result.get("rateLimitsByLimitId", {}).get("codex", result["rateLimits"])
                if case == "weekly-only":
                    self.assertIsNone(snapshot["secondary"])
                    self.assertEqual(snapshot["primary"]["windowDurationMins"], 10080)
                elif case == "null":
                    self.assertEqual(snapshot, {"primary": None, "secondary": None})
                elif case == "five-first":
                    self.assertEqual(snapshot["primary"]["windowDurationMins"], 300)
                elif case == "weekly-full":
                    self.assertEqual(snapshot["primary"]["usedPercent"], 100)
                else:
                    self.assertNotIn("rateLimitsByLimitId", result)

    def test_interleaved_notifications_and_typed_response_ids(self):
        self.assertIsInstance(self.probe("wrong-type-id"), dict)
        self.assertEqual(
            [row["method"] for row in self.logged()[1:]],
            ["initialize", "initialized", "account/rateLimits/read"],
        )
        self.assertIsNone(self.probe("init-wrong-type-id", timeout=0.15))
        self.assertEqual([row["method"] for row in self.logged()[1:]], ["initialize"])

    def test_failures_return_null_and_reap_the_app_server(self):
        cases = (
            "error",
            "init-error",
            "malformed",
            "oversize",
            "exit",
            "silent",
            "wrong-id",
            "null-result",
            "invalid-utf8",
            "partial-exit",
            "stderr",
            "ignore-term-silent",
        )
        for case in cases:
            with self.subTest(case=case):
                self.assertIsNone(self.probe(case, timeout=0.15))
        self.assertIsNone(self.probe(PATH=str(self.work)))
        self.assertEqual(self.logged(), [])

    def test_success_cleanup_kills_a_child_that_ignores_termination(self):
        self.assertIsInstance(self.probe("ignore-term"), dict)

    def test_invalid_timeout_and_arguments_return_null_without_spawning(self):
        for argv in (
            ("--timeout", "0"),
            ("--timeout", "-1"),
            ("--timeout", "11"),
            ("--timeout", "nan"),
            ("--timeout", "inf"),
            ("--timeout", "invalid"),
            ("--unknown",),
        ):
            with self.subTest(argv=argv):
                result = subprocess.run(
                    (sys.executable, "-I", str(self.script), *argv),
                    cwd=self.work,
                    env=self.env,
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                self.assertEqual(
                    (result.returncode, result.stdout, result.stderr), (0, "null\n", "")
                )
                self.assertEqual(self.logged(), [])

    def test_native_environment_is_forwarded_without_config_writes(self):
        self.assertIsInstance(
            self.probe(FAKE_ADDITION="configured", NO_COLOR="configured", PAGER="configured"), dict
        )
        env = self.logged()[0]["env"]
        self.assertEqual(
            (env["HOME"], env["CODEX_HOME"], env["FAKE_ADDITION"], env["NO_COLOR"], env["PAGER"]),
            (str(self.home), str(self.codex_home), "configured", "configured", "configured"),
        )
