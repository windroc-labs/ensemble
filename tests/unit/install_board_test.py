"""Installer's embedded board checks stay aligned with the router without running the shell."""

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from ensemble.orchestrator.engine.config import REQUIRED
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
        self.assertIn("linear.manager_key", covered)

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
