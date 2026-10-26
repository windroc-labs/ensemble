"""Installer's embedded board checks stay aligned with the router without running the shell."""

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from ensemble.orchestrator.engine.config import LINEAR_KEYS, REQUIRED, AppKey
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

    def slots(self, text, stages="pm\tdesign"):
        """linear_slots over a config.local.toml holding text: (stdout lines, exit message or None)."""
        path = Path(self.home, "config.toml")
        path.write_text(text)
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["-c", str(path), stages]), redirect_stdout(out):
            try:
                exec(embedded("linear_slots"), {})
            except SystemExit as stop:
                return out.getvalue().splitlines(), str(stop.code)
        return out.getvalue().splitlines(), None

    def test_slots_list_each_app_harness_and_manager_first(self):
        self.assertEqual(
            self.slots(""),
            (
                [
                    f"{slot}\tlinear-app-{slot}-token\tlinear-app-{slot}-secret"
                    for slot in ("harness", "manager", "pm")
                ],
                None,
            ),
        )

    def test_slots_refuse_unknown_linear_keys_pointing_to_the_upgrade_steps(self):
        self.assertEqual(
            self.slots('[orchestrator.linear]\ntypo = 1\nother = ""\n'),
            (
                [],
                "install.sh: config.local.toml: unknown keys in [linear]: other, typo; "
                "see docs/upgrading.md#linear-apps",
            ),
        )

    def test_slots_refuse_unknown_app_keys_naming_table_and_key(self):
        for text, table in (
            ('[orchestrator.linear.roles.pm]\nkey = "x"\n', "linear.roles.pm"),
            ('[orchestrator.linear.harness]\ntypo = "x"\ntoken = "y"\n', "linear.harness"),
        ):
            with self.subTest(table=table):
                _, stop = self.slots(text)
                self.assertEqual(
                    stop,
                    f"install.sh: config.local.toml: unknown keys in [{table}]: "
                    + ("key" if "roles" in table else "token, typo")
                    + "; see docs/upgrading.md#linear-apps",
                )

    def test_slots_refuse_a_non_plain_keychain_service(self):
        _, stop = self.slots('[orchestrator.linear.manager]\ntoken_service = "a b"\n')
        self.assertEqual(
            stop,
            "install.sh: config.local.toml: manager keychain service 'a b': not a plain keychain name",
        )

    def test_slots_know_exactly_the_orchestrators_linear_keys(self):
        values = {}
        path = Path(self.home, "config.toml")
        path.write_text("")
        with (
            mock.patch.object(sys, "argv", ["-c", str(path), "pm\tdesign"]),
            redirect_stdout(io.StringIO()),
        ):
            exec(embedded("linear_slots"), values)
        self.assertEqual(values["KNOWN"], LINEAR_KEYS)

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
