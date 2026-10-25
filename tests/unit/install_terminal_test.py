"""Installer fake terminal shutdown, with no threads or real file descriptors."""

import ast
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.case import REPO, Case


class InstallTerminalTest(Case):
    def reader(self, terminal, ready, read):
        path = Path(REPO, "tests/integration/install_integration_test.py")
        tree = ast.parse(path.read_text())
        tty = next(
            node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Tty"
        )
        method = next(
            node for node in tty.body if isinstance(node, ast.FunctionDef) and node.name == "read"
        )
        namespace = {
            "os": SimpleNamespace(read=read),
            "select": SimpleNamespace(select=ready),
            "time": SimpleNamespace(sleep=mock.Mock()),
        }
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), "exec"), namespace)
        namespace["read"](terminal)

    def test_no_ready_bytes_allows_shutdown_without_blocking_read(self):
        terminal = SimpleNamespace(master=7, done=False, text="", lock=nullcontext())

        def ready(*_args):
            terminal.done = True
            return [], [], []

        read = mock.Mock(side_effect=AssertionError("read would block with no ready bytes"))
        self.reader(terminal, ready, read)
        read.assert_not_called()
        self.assertEqual(terminal.text, "")

    def test_ready_bytes_are_collected_before_shutdown(self):
        terminal = SimpleNamespace(master=7, done=False, text="", lock=nullcontext())

        def read(*_args):
            terminal.done = True
            return b"prompt"

        self.reader(terminal, mock.Mock(return_value=([7], [], [])), read)
        self.assertEqual(terminal.text, "prompt")
