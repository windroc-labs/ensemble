"""The image command exposes issues without host orchestration or local fallback."""

import importlib
import io
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from tests.orchestrator.case import Case


class WorkerCli(Case):
    def test_issue_help_uses_process_arguments_without_access(self):
        worker = importlib.import_module("ensemble.orchestrator.worker_cli")
        out = io.StringIO()
        with (
            mock.patch("sys.argv", ["ensemble-orchestrator", "issue", "--help"]),
            redirect_stdout(out),
            self.assertRaises(SystemExit) as result,
        ):
            worker.main()
        self.assertEqual(result.exception.code, 0)
        self.assertIn("read", out.getvalue())

    def test_absent_board_fails_without_host_configuration(self):
        worker = importlib.import_module("ensemble.orchestrator.worker_cli")
        out, err = io.BytesIO(), io.StringIO()
        with (
            mock.patch("os.stat", side_effect=FileNotFoundError),
            mock.patch("builtins.open", side_effect=AssertionError("host config read")),
        ):
            code = worker.main(["issue", "read", "TASK-2"], out=out, err=err)
        self.assertEqual(code, 2)
        self.assertEqual(out.getvalue(), b"")
        self.assertIn("manager", err.getvalue())

    def test_host_commands_are_unavailable(self):
        worker = importlib.import_module("ensemble.orchestrator.worker_cli")
        for command in ("router", "promote", "credentials", "status"):
            with (
                self.subTest(command=command),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as result,
            ):
                worker.main([command])
            self.assertEqual(result.exception.code, 2)
