"""clients/codex/tui/start.py run as its own process: `python -P -s -m` its module."""

import json
import os
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

from ensemble.session.clients.codex import tui
from ensemble.session.clients.codex.common import app_server
from ensemble.session.clients.codex.tui import start as tui_start
from tests.session import case
from tests.session.case import Case
from tests.session.unit.clients.codex.support import Base

MODULE = tui_start.__name__
INSPECT = """import dataclasses, json, runpy, sys, typing
module, data = sys.argv[1:]
sys.argv = [module, data]
before = list(sys.path)
try:
    runpy.run_module(module, run_name="__main__", alter_sys=True)
except SystemExit as error:
    status = error.code
from ensemble.session.clients import common as shared
from ensemble.session.clients.codex import common, tui
from ensemble.session.clients.codex.common import app_server
from ensemble.session.clients.codex.common import types
from ensemble.session.clients.codex.tui import start
print(json.dumps([
    status,
    sys.path == before,
    type(next(shared.scan(["--add-dir=/x"], common.VALUED))) is shared.Flag,
    dataclasses.is_dataclass(common.SessionState),
    common.SessionState is types.SessionState,
    typing.get_type_hints(common.SessionState)["snapshot"] is types.Snapshot,
    start.common is common,
    tui.SessionStore is common.SessionStore,
    tui.RecoveryError is common.RecoveryError,
    start.app_server is app_server,
    start.HOOKS is tui.HOOKS,
    tui.on_hook.__module__ == tui.__name__,
    sorted(name for name in sys.modules if name.startswith("_ensemble_codex_")),
    common.__file__,
    sys.modules["ensemble.utils"].__file__,
]))
"""
ROOTS = (case.SRC["session"], case.SRC["utils"])


class ModuleRunTest(Case):
    """start runs as `python -P -s -m` its module, its packages from PYTHONPATH."""

    def data(self):
        return {
            "executable": sys.executable,
            "selection": [],
            "prompt": "",
            "hook": [sys.executable],
            "workdir": self.home,
            "sid": "module",
            "store": "/uncreated-ensemble-state/recovery/codex",
            "permission": [],
            "flags": [],
        }

    def run_module(self, command, cwd=None):
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(ROOTS)}
        return subprocess.run(command, capture_output=True, text=True, cwd=cwd, env=env)

    def test_the_module_uses_canonical_modules_dataclasses_and_flag_type(self):
        result = self.run_module(
            [sys.executable, "-P", "-s", "-c", INSPECT, MODULE, json.dumps(self.data())]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "codex tui: Codex tui launch: missing recovery state\n")
        values = json.loads(result.stdout)
        self.assertEqual(values[:13], [1, *([True] * 11), []])
        self.assertTrue(values[13].startswith(ROOTS[0] + os.sep))
        self.assertTrue(values[14].startswith(ROOTS[1] + os.sep))

    def test_launch_data_errors_are_one_stderr_line_and_status_1_from_the_module(self):
        for name, data, error in (
            ("reaches launch validation", json.dumps(self.data()), "missing recovery state"),
            (
                "the retired import_roots field",
                json.dumps({**self.data(), "import_roots": list(ROOTS)}),
                "invalid launch fields",
            ),
            ("bad launch data", "not-json", "malformed launch data"),
        ):
            with self.subTest(name):
                result = self.run_module([sys.executable, "-P", "-s", "-m", MODULE, data])
                self.assertEqual(
                    (result.returncode, result.stdout, result.stderr),
                    (1, "", f"codex tui: Codex tui launch: {error}\n"),
                )

    def test_the_module_decodes_launch_data_once(self):
        code = """import json, runpy, sys
from unittest import mock
module, data = sys.argv[1:]
sys.argv = [module, data]
with mock.patch("json.loads", wraps=json.loads) as decode:
    try:
        runpy.run_module(module, run_name="__main__", alter_sys=True)
    except SystemExit as error:
        status = error.code
print(json.dumps([status, decode.call_count]))
"""
        result = self.run_module(
            [sys.executable, "-P", "-s", "-c", code, MODULE, json.dumps(self.data())]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "codex tui: Codex tui launch: missing recovery state\n")
        self.assertEqual(json.loads(result.stdout), [1, 1])

    def test_a_cwd_decoy_is_not_imported(self):
        decoy = pathlib.Path(self.home) / "untrusted"
        decoy.mkdir()
        (decoy / "ensemble.py").write_text('raise RuntimeError("untrusted ensemble")\n')
        (decoy / "common.py").write_text('raise RuntimeError("untrusted sessions")\n')
        result = self.run_module(
            [sys.executable, "-P", "-s", "-c", INSPECT, MODULE, json.dumps(self.data())],
            cwd=decoy,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)[:13], [1, *([True] * 11), []])

    def test_package_imports_keep_sys_path_and_share_canonical_modules(self):
        code = """import json, sys
before = list(sys.path)
from ensemble.session.clients import common as shared
from ensemble.session.clients.codex import common, tui
from ensemble.session.clients.codex.tui import start as tui_start
print(json.dumps([
    sys.path == before,
    type(next(shared.scan(["--add-dir=/x"], common.VALUED))) is shared.Flag,
    tui_start.common is common,
    tui.SessionStore is common.SessionStore,
    sorted(name for name in sys.modules if name.startswith("_ensemble_codex_")),
]))
"""
        result = subprocess.run(
            [sys.executable, "-P", "-s", "-c", code],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(json.loads(result.stdout), [True, True, True, True, []])

    def test_the_launch_argument_is_required_and_bounded_before_decoding(self):
        result = self.run_module([sys.executable, "-P", "-s", "-m", MODULE])
        self.assertEqual(
            (result.returncode, result.stdout, result.stderr),
            (1, "", "codex tui: Codex tui launch: expected bounded launch data\n"),
        )
        with (
            mock.patch("json.loads") as decode,
            self.assertRaisesRegex(tui_start.StartError, "expected bounded launch data"),
        ):
            tui_start._decode_launch_data([" " * (tui_start.ANSWER_MAX + 1)])
        decode.assert_not_called()

    def test_a_lone_surrogate_is_one_line_and_failure_status(self):
        code = """import runpy, sys
sys.argv = [sys.argv[1], chr(0xD800)]
runpy.run_module(sys.argv[0], run_name="__main__", alter_sys=True)
"""
        result = self.run_module([sys.executable, "-P", "-s", "-c", code, MODULE])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(
            result.stderr,
            "codex tui: 'utf-8' codec can't encode character '\\ud800' in position 0: surrogates not allowed\n",
        )


class StartTest(Base):
    def test_shared_schema_preserves_hooks_in_isolated_scripts_and_client(self):
        expected = (
            ("SessionStart", "sessionStart", "activity"),
            ("UserPromptSubmit", "userPromptSubmit", "turn_started"),
            ("PostToolUse", "postToolUse", "activity"),
            ("PermissionRequest", "permissionRequest", "blocked"),
            ("PreToolUse", "preToolUse", "blocked"),
            ("Stop", "stop", "turn_end"),
            ("Interrupt", "interrupt", "turn_end"),
        )
        self.assertEqual(tui.HOOKS, expected)
        self.assertEqual(self.client.tui().hooks, {label: kind for label, _, kind in expected})
        self.assertEqual(tui_start.HOOKS, expected)
        self.assertEqual(set(tui.CodexTui.hooks), {label for label, _, _ in expected})
        code = """import json, sys
sys.path[:0] = json.loads(sys.argv[1])
from ensemble.session.clients.codex.tui import HOOKS
print(json.dumps(HOOKS))
"""
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                code,
                json.dumps([case.SRC["session"], case.SRC["utils"]]),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(json.loads(result.stdout), [list(row) for row in expected])

    def test_standalone_account_probe_uses_stdlib(self):
        script = """import json, runpy, sys
imports = set()
def record(event, args):
    if event == "import":
        imports.add(args[0].partition(".")[0])
sys.addaudithook(record)
runpy.run_path(sys.argv[1])
print(json.dumps(sorted((imports & sys.modules.keys()) - sys.stdlib_module_names)))
"""
        result = subprocess.run(
            [sys.executable, "-I", "-c", script, str(pathlib.Path(app_server.__file__))],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(json.loads(result.stdout), [])


if __name__ == "__main__":
    unittest.main()
