"""Codex session usage queries and environment isolation through a fake app-server."""

import json
import os
import shlex
import sys
import time
from pathlib import Path
from unittest import mock

from ensemble.session.clients.codex import CodexClient
from ensemble.session.engine.types import ConfigError, Headroom, Window
from tests.session.case import REPO, Case
from tests.session.integration.clients.codex import fake_app_server
from tests.utils.sandbox import scratch

FIVE, WEEK = Window(62, 2000), Window(41, 4000)
BOTH = [Headroom("default", 38)]  # the most-used window's points left under 100


class UsageIntegrationTest(Case):
    def setUp(self):
        self.root = Path(scratch(self, os.environ.get("ENSEMBLE_TEST_TMP", REPO)))
        self.home, self.work = (self.root / name for name in ("home", "work"))
        self.codex_home = self.home / ".codex"
        for path in (self.work, self.codex_home):
            path.mkdir(parents=True, exist_ok=True)
        self.common = CodexClient({}).common
        self.bin = self.root / "bin"
        self.bin.mkdir()
        executable = self.bin / "codex"
        executable.write_text(
            "#!/bin/sh\nexec "
            + shlex.quote(sys.executable)
            + " "
            + shlex.quote(fake_app_server.__file__)
            + ' "$@"\n'
        )
        executable.chmod(0o755)
        self.log = self.root / "probe.jsonl"
        self.enterContext(
            mock.patch.dict(
                os.environ,
                {
                    "HOME": str(self.home),
                    "CODEX_HOME": str(self.codex_home),
                    "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                    "CODEX_THREAD_ID": "inherited",
                },
            )
        )

    def probe(self, case="normal", **env):
        self.configure_session(
            {
                "codex": {
                    "implementation": "ensemble.session.clients.codex:CodexClient",
                    "env": {"FAKE_LOG": str(self.log), "FAKE_CASE": case, **env},
                }
            }
        )
        started = time.monotonic()
        result = self.session_cls.usage("codex")
        self.assertLess(time.monotonic() - started, 12)
        if self.log.exists():
            rows = [json.loads(line) for line in self.log.read_text().splitlines()]
            with self.assertRaises(ProcessLookupError):
                os.kill(rows[0]["pid"], 0)
        return result

    def test_one_light_probe_matches_ids_and_maps_window_duration(self):
        self.assertEqual(self.probe(), BOTH)
        rows = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(rows[0]["argv"], ["app-server"])
        self.assertIsNone(rows[0]["marker"])
        self.assertEqual(
            [row["method"] for row in rows[1:]],
            ["initialize", "initialized", "account/rateLimits/read"],
        )
        self.assertEqual(rows[1]["id"], 1)
        self.assertEqual(rows[3]["id"], 2)
        self.assertNotEqual(rows[0]["ppid"], os.getpid())
        self.assertEqual(rows[0]["cwd"], str(self.home / ".ensemble" / "probe"))
        self.assertEqual(rows[0]["env"]["PWD"], rows[0]["cwd"])
        self.assertFalse((self.work / "run.jsonl").exists())

    def test_each_reply_maps_to_its_windows_and_failures_are_unknown_and_cleaned_up(self):
        for case, expected in (
            ("weekly-only", [Headroom("default", 59)]),
            ("error", [Headroom("default", None)]),
            ("ignore-term", BOTH),
        ):
            with self.subTest(case=case):
                self.log.unlink(missing_ok=True)
                self.assertEqual(self.probe(case), expected)
        self.log.unlink(missing_ok=True)
        with self.assertRaisesRegex(ConfigError, "missing or not executable"):
            self.probe(PATH=str(self.work))
        self.assertFalse(self.log.exists())

    def test_native_environment_filters_markers_and_keeps_configured_overrides(self):
        common = self.common
        inherited = {key: "inherited" for key in (*common.UNSET, *common.DEFAULTS)}
        with mock.patch.dict(os.environ, inherited):
            self.assertEqual(self.probe(FAKE_ADDITION="configured", PAGER="configured"), BOTH)
        env = json.loads(self.log.read_text().splitlines()[0])["env"]
        for key in common.UNSET:
            self.assertIsNone(env[key], key)
        for key, value in common.DEFAULTS.items():
            self.assertEqual(env[key], "configured" if key == "PAGER" else value, key)
        self.assertEqual(
            (env["CODEX_HOME"], env["FAKE_ADDITION"]), (str(self.codex_home), "configured")
        )
