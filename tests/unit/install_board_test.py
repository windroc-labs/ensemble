"""Installer's embedded board checks stay aligned with the router without running the shell."""

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from ensemble.orchestrator.engine.config import REQUIRED, AppKey
from ensemble.orchestrator.engine.flow.workflow import Workflow
from tests.case import REPO, Case


def embedded(function):
    text = Path(REPO, "scripts/install.sh").read_text()
    body = text.split(f"{function}() {{\n", 1)[1].split("\n}\n", 1)[0]
    return body.split("python3 -I -c '\n", 1)[1].split("' ", 1)[0]


class BoardContract(Case):
    def test_complete_covers_every_orchestrator_required_key(self):
        path = Path(self.home, "config.toml")
        path.write_text("")
        values = {}
        with (
            mock.patch.object(sys, "argv", ["-c", str(path), "reviewer\tbuild"]),
            self.assertRaises(SystemExit),
        ):
            exec(embedded("board_complete"), values)
        covered = {
            "linear." + ".".join("*" if key == "reviewer" else key for key in keys)
            for keys in values["required"]
        }
        self.assertLessEqual(set(REQUIRED), covered)
        self.assertLessEqual({f"linear.manager.{key}" for key in AppKey}, covered)
        self.assertNotIn("linear.manager_key", covered)

    def test_slots_make_the_manager_an_app_and_refuse_manager_key(self):
        path = Path(self.home, "config.toml")
        path.write_text("")
        out = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["-c", str(path), "pm\tdesign"]),
            redirect_stdout(out),
        ):
            exec(embedded("linear_slots"), {})
        self.assertEqual(
            out.getvalue().splitlines(),
            [
                f"{slot}\tlinear-app-{slot}-token\tlinear-app-{slot}-secret"
                for slot in ("harness", "manager", "pm")
            ],
        )
        path.write_text('[orchestrator.linear]\nmanager_key = "linear-manager"\n')
        with (
            mock.patch.object(sys, "argv", ["-c", str(path), "pm\tdesign"]),
            self.assertRaises(SystemExit) as cm,
        ):
            exec(embedded("linear_slots"), {})
        self.assertIn("unknown keys in [linear]: manager_key", str(cm.exception.code))
        self.assertIn("docs/credentials.md#linear-manager-app", str(cm.exception.code))

    def test_stage_snapshot_is_passed_to_board_helpers(self):
        script = Path(REPO, "scripts/install.sh").read_text()
        self.assertEqual(script.count("$(stage_roles)"), 1)
        board = script.split("board_config() {\n", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("$(stage_roles)", board)

    def test_stage_roles_match_the_workflows_roles_and_inputs(self):
        path = Path(self.home, "workflow.toml")
        config = Path(self.home, "config.toml")
        path.write_text(
            '[[stage]]\nrole = "reviewer"\ninput = "build"\nnext = "engineer"\n'
            '[[stage]]\nrole = "engineer"\ninput = "build"\n'
        )
        for custom in (False, True):
            with self.subTest(custom=custom):
                config.write_text(f'[orchestrator]\nworkflow = "{path}"\n' if custom else "")
                output = io.StringIO()
                packaged = Path(REPO, "src/ensemble/orchestrator/engine/flow/workflow.toml")
                with (
                    mock.patch.object(sys, "argv", ["-c", str(config), str(packaged)]),
                    redirect_stdout(output),
                ):
                    exec(embedded("stage_roles"), {})
                stages = Workflow().load(str(path) if custom else None)
                self.assertEqual(
                    output.getvalue().splitlines(),
                    [f"{role}\t{stage.input}" for role, stage in stages.items()],
                )
