"""Installer's embedded board checks stay aligned with the router without running the shell."""

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from ensemble.orchestrator.engine.config import LINEAR_KEYS, REQUIRED, AppKey
from ensemble.orchestrator.engine.flow.workflow import Workflow
from tests.case import REPO, Case
from tests.unit.install_script import program


class BoardContract(Case):
    def test_complete_covers_every_orchestrator_required_key(self):
        path = Path(self.home, "config.toml")
        path.write_text("")
        values = {}
        with (
            mock.patch.object(sys, "argv", ["-c", str(path), "reviewer\tbuild"]),
            self.assertRaises(SystemExit),
        ):
            exec(program("board_complete"), values)
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
                exec(program("linear_slots"), {})
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
                "remove them, then rerun install.sh --board linear",
            ),
        )

    def test_slots_refuse_unknown_app_keys_naming_table_and_key(self):
        for text, table, names in (
            ('[orchestrator.linear.roles.pm]\ntoken = "x"\n', "linear.roles.pm", "token"),
            ('[orchestrator.linear.roles.writer]\ntoken = "x"\n', "linear.roles.writer", "token"),
            (
                '[orchestrator.linear.harness]\ntypo = "x"\ntoken = "y"\n',
                "linear.harness",
                "token, typo",
            ),
        ):
            with self.subTest(table=table):
                _, stop = self.slots(text)
                self.assertEqual(
                    stop,
                    f"install.sh: config.local.toml: unknown keys in [{table}]: {names}"
                    "; remove them, then rerun install.sh --board linear",
                )

    def test_slots_refuse_a_linear_setting_that_is_no_table(self):
        self.assertEqual(
            self.slots('[orchestrator]\nlinear = "abc"\n'),
            ([], "install.sh: config.local.toml: [linear] must be a table"),
        )

    def test_slots_name_a_bad_keychain_service_by_field_never_by_value(self):
        for text, said in (
            (
                '[orchestrator.linear.manager]\ntoken_service = "a b"\n',
                "linear.manager.token_service: not a plain keychain name",
            ),
            (
                '[orchestrator.linear.roles.pm]\nsecret_service = "pasted secret"\n',
                "linear.roles.pm.secret_service: not a plain keychain name",
            ),
            (
                '[orchestrator.linear.harness]\nsecret_service = "secret-1"\n'
                '[orchestrator.linear.manager]\nsecret_service = "secret-1"\n',
                "linear.manager.secret_service is also linear.harness.secret_service",
            ),
        ):
            with self.subTest(said=said):
                _, stop = self.slots(text)
                self.assertEqual(stop, f"install.sh: config.local.toml: {said}")
                self.assertNotIn("a b", stop)
                self.assertNotIn("pasted secret", stop)
                self.assertNotIn("secret-1", stop)

    def test_slots_know_exactly_the_orchestrators_linear_keys(self):
        values = {}
        path = Path(self.home, "config.toml")
        path.write_text("")
        with (
            mock.patch.object(sys, "argv", ["-c", str(path), "pm\tdesign"]),
            redirect_stdout(io.StringIO()),
        ):
            exec(program("linear_slots"), values)
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
                    exec(program("stage_roles"), {})
                stages = Workflow().load(str(path) if custom else None)
                self.assertEqual(
                    output.getvalue().splitlines(),
                    [f"{role}\t{stage.input}" for role, stage in stages.items()],
                )
