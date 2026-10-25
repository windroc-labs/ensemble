from pathlib import Path
from unittest import mock

from tests.case import Case
from tests.unit_imports import CONFIG, _unit_files, main


class UnitImports(Case):
    def test_selection_preserves_unit_paths_github_tests_and_allowlist(self):
        root = Path(self.home)
        selected = (
            "manager/tests/unit/engine/a_test.py",
            "session/tests/unit/b_test.py",
            "tests/unit/helper.py",
            "nested/unit/tests/c.py",
            ".github/escape_test.py",
        )
        excluded = (
            "tests/integration/a_test.py",
            ".github/run_tests.py",
            ".github/nested/a_test.py",
            "unit/only.py",
            "tests/notunit/a_test.py",
            "tests/unit/readme.md",
        )
        for name in (*selected, *excluded):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("")
        self.assertEqual(_unit_files(root), sorted(str(root / name) for name in selected))
        with mock.patch.dict("tests.guards.UNIT_ISOLATION_ALLOWED", {selected[0]: "fixture"}):
            self.assertEqual(_unit_files(root), sorted(str(root / name) for name in selected[1:]))

    def test_scan_uses_shared_config_and_propagates_ruff_status(self):
        root = Path(self.home)
        path = root / ".github" / "example_test.py"
        path.parent.mkdir()
        path.write_text("")
        for code in (0, 1, 2):
            with (
                self.subTest(code=code),
                mock.patch("subprocess.run", return_value=mock.Mock(returncode=code)) as run,
            ):
                self.assertEqual(main(root), code)
                run.assert_called_once_with(
                    [
                        "ruff",
                        "check",
                        "--isolated",
                        "--no-cache",
                        "--select",
                        "TID251",
                        "--config",
                        CONFIG,
                        "--output-format",
                        "github",
                        str(path),
                    ],
                    timeout=60,
                )

    def test_empty_selection_does_not_scan_the_working_directory(self):
        with mock.patch("subprocess.run") as run:
            self.assertEqual(main(Path(self.home)), 0)
        run.assert_not_called()
