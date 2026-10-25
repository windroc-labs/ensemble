"""Shared Codex launch plans and launch fixtures."""

import dataclasses
import json
import os
import sys
from unittest import mock

from ensemble.session.clients.codex.common.types import Snapshot
from ensemble.session.clients.codex.tui import HookPlan, LaunchPlan
from tests.session.case import SRC, Case
from tests.session.unit.clients.codex.support import Base, committed_tui

ROOTS_JSON = json.dumps([SRC["session"], SRC["utils"]])
HOOK = (sys.executable, "-I", "/protected/driver_hook.py", "/protected/session", ROOTS_JSON)


class LaunchCase(Base):
    def launch(self, spec, *, resume=False):
        with mock.patch("shutil.which", return_value="/protected/codex"):
            return committed_tui(
                self.client, dataclasses.replace(spec, prompt="prompt", resume=resume), HOOK
            ).argv[-1]


class PlanCase(Case):
    def setUp(self):
        self.snapshot = Snapshot(
            cwd="/work",
            project=True,
            dirs=("/granted",),
            commands=(),
            model="configured",
            effort=None,
            codex_home="/client/codex",
            flags=(
                "-m",
                "requested",
                "-c",
                'model_verbosity="low"',
                "--enable",
                "web",
                "--",
                "--config=literal",
                "--disable",
                "literal",
            ),
            env={},
        )
        self.plan = LaunchPlan(
            executable="/protected/codex",
            selection=("resume", "saved-native"),
            prompt="prompt\nwith Unicode: 字",
            hook=HookPlan(
                (os.path.realpath(sys.executable), "-I", "/protected/mode hook.py", "/session dir")
            ),
            workdir="/work",
            sid="caller-id",
            store="/private-state/recovery/codex",
            permission=(),
            flags=(),
        )
        self.frontend = (
            "/protected/codex",
            "resume",
            "saved-native",
            "--no-daemon",
            "-C",
            "/work",
            "-c",
            'tui.notification_method="bel"',
        )
