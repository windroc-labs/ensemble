"""Orchestrator module loading in a clean interpreter."""

import json
import os
import subprocess
from pathlib import Path

from tests.case import ORCH_ROOT, PY, Case, child_env

PROBE = "import importlib,json,sys\nprint(json.dumps({n:importlib.import_module(n).__file__ for n in sys.argv[1:]}))\n"


class RealTree(Case):
    def test_orchestrator_loads_from_its_root(self):
        package = os.path.join(ORCH_ROOT, "ensemble", "orchestrator")
        names = sorted(
            ".".join(path.relative_to(ORCH_ROOT).with_suffix("").parts).removesuffix(".__init__")
            for path in Path(package).rglob("*.py")
        )
        self.assertIn("ensemble.orchestrator.engine.router", names)
        out = subprocess.run(
            [*PY, "-c", PROBE, *names],
            env=child_env(),
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        loaded = json.loads(out)
        real = os.path.realpath(package)
        for name in names:
            self.assertEqual(os.path.commonpath([real, os.path.realpath(loaded[name])]), real)
