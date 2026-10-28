"""A TUI client installed outside the session's package roots (its own directory, importing only
ensemble.session.extension) declares its hook callback with its own root: `ensemble-session hook`, on
only the session's source roots, finds and runs it; undeclared, the callback is not found.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile

from tests.session.case import SRC, Case
from tests.session.tui_hook_support import COMMAND, state, write_state
from tests.utils.jsonl import read_jsonl

CLIENT = """import os

from ensemble.session.extension import HookCallback, HookKind, Tui

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ExternalTui(Tui):
    hooks = {"stop": HookKind.TURN_END}

    def launch(self, spec, hook):
        raise NotImplementedError

    def reply(self, payload):
        return ""

    def hook_callback(self):
        return HookCallback("external_tui.hooks", {"tag": "external"}, roots=(ROOT,))
"""
CALLBACK = """def on_hook(call):
    return {
        **call.payload,
        "seen": [call.label, call.sid, call.workdir, call.events, dict(call.state)],
    }
"""


class ExternalTuiHookTest(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.installed = os.path.join(self.root, "installed")
        package = os.path.join(self.installed, "external_tui")
        os.makedirs(package)
        for name, text in (("__init__.py", CLIENT), ("hooks.py", CALLBACK)):
            with open(os.path.join(package, name), "w") as f:
                f.write(text)
        self.dir = os.path.join(self.root, "s1")
        os.mkdir(self.dir, 0o700)
        self.state = {**state(self.root, None), "words": {}}

    def declared(self) -> dict:
        path = os.path.join(self.installed, "external_tui", "__init__.py")
        spec = importlib.util.spec_from_file_location("external_tui", path)
        if spec is None or spec.loader is None:
            raise ImportError(f"{path}: cannot load Python module")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.ExternalTui().hook_callback().fields()

    def hook(self) -> str:
        res = subprocess.run(
            [COMMAND, "hook", self.dir, "stop"],
            input=json.dumps({"text": "hi"}).encode(),
            capture_output=True,
            env={
                "HOME": self.root,
                "PATH": os.pathsep.join((os.path.dirname(sys.executable), os.defpath)),
            },
            timeout=60,
        )
        self.assertEqual((res.returncode, res.stdout), (0, b""), res.stderr)
        return res.stderr.decode()

    def records(self) -> list[dict]:
        return read_jsonl(os.path.join(self.dir, "hooks.jsonl"), missing_ok=True)

    def test_its_declared_callback_runs_from_its_own_root(self):
        declared = self.declared()
        self.assertEqual(
            declared,
            {
                "module": "external_tui.hooks",
                "state": {"tag": "external"},
                "roots": [self.installed],
            },
        )
        self.assertNotIn(self.installed, (SRC["session"], SRC["utils"]))
        write_state(self.dir, {**self.state, "client_hook": declared})
        self.assertEqual(self.hook(), "")
        self.assertEqual(
            [record["payload"] for record in self.records()],
            [
                {
                    "text": "hi",
                    "seen": ["stop", self.state["sid"], self.root, None, {"tag": "external"}],
                }
            ],
        )

    def test_without_its_root_the_callback_is_refused_and_nothing_journaled(self):
        undeclared = {**self.declared(), "roots": []}
        write_state(self.dir, {**self.state, "client_hook": undeclared})
        self.assertRegex(
            self.hook(),
            r"\Atui_hook: client hook external_tui.hooks: cannot find Python module\n\Z",
        )
        self.assertEqual(self.records(), [])
