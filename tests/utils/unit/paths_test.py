"""
src/ensemble/utils/paths.py: real_dirs, under, temp_dirs and printable_abs.
"""

import os
import sys
import unittest
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

from ensemble.utils.paths import (
    DOCKER_STATE,
    docker_home,
    docker_home_of,
    import_env,
    module_roots,
    overlaps,
    printable_abs,
    real_dirs,
    source_path,
    temp_dirs,
    under,
)
from tests.utils.case import Case
from tests.utils.process_fixtures import DEVNULL, TimeoutExpired


class PathsTest(Case):
    def setUp(self):
        self.root = Path(os.path.realpath(self.home))
        self.home = self.root / "home"
        self.home.mkdir()
        self.alias = self.root / "alias"
        self.alias.symlink_to(self.home)
        self.enterContext(mock.patch.dict(os.environ, {"HOME": str(self.home)}))

    def test_real_dirs_expands_home_resolves_symlinks_and_deduplicates(self):
        paths = ["~/project", str(self.alias / "project"), str(self.home / "other")]
        self.assertEqual(
            real_dirs(paths, "roots"), {str(self.home / "project"), str(self.home / "other")}
        )
        self.assertEqual(real_dirs([], "roots"), frozenset())

    def test_real_dirs_rejects_invalid_collections_and_paths(self):
        for paths in (
            None,
            "~/project",
            (str(self.home),),
            [1],
            ["relative"],
        ):
            with self.subTest(paths=paths), self.assertRaisesRegex(ValueError, "^roots:"):
                real_dirs(paths, "roots")

    def test_real_dirs_rejects_an_unknown_user(self):
        with (
            mock.patch("pwd.getpwnam", side_effect=KeyError("unknown-task318-user")),
            self.assertRaisesRegex(ValueError, "^roots:"),
        ):
            real_dirs(["~unknown-task318-user/project"], "roots")

    def test_under_returns_the_first_real_root_holding_the_path(self):
        project = str(self.home / "project")
        for path, roots, want in (
            (project, [self.alias, self.root], str(self.home)),
            (project, [self.root, self.alias], str(self.root)),
            (str(self.root / "home-other"), [self.home], None),
            (str(self.home), [self.alias], str(self.home)),
            (str(self.home), [], None),
        ):
            with self.subTest(path=path, roots=roots):
                self.assertEqual(under(path, roots), want)

    def test_temp_dirs_collects_environment_python_system_and_darwin_roots(self):
        value = SimpleNamespace(returncode=0, stdout=str(self.root / "darwin") + "\n", stderr="")
        with (
            mock.patch.dict(os.environ, {"TMPDIR": str(self.alias)}),
            mock.patch("tempfile.gettempdir", return_value=str(self.home)),
            mock.patch("subprocess.run", return_value=value) as run,
        ):
            actual = temp_dirs()
        self.assertEqual(
            actual,
            tuple(
                dict.fromkeys(
                    os.path.realpath(path)
                    for path in (self.alias, self.home, "/tmp", "/var/tmp", self.root / "darwin")
                )
            ),
        )
        run.assert_called_once_with(
            ["getconf", "DARWIN_USER_TEMP_DIR"],
            capture_output=True,
            text=True,
            timeout=60,
            stdin=DEVNULL,
        )

    def test_temp_dirs_keeps_roots_when_getconf_is_unavailable_or_times_out(self):
        for error in (OSError("no getconf"), TimeoutExpired("getconf", 60)):
            with self.subTest(error=error), mock.patch("subprocess.run", side_effect=error):
                self.assertIn(os.path.realpath("/var/tmp"), temp_dirs())


class PrintableAbs(Case):
    def test_a_printable_absolute_str_only(self):
        for value in ("/", "/a/b", "/a b/é", "/x/../y"):
            with self.subTest(value=value):
                self.assertTrue(printable_abs(value))
        for value in (
            "",
            "rel",
            "./a",
            "~/a",
            "/c\n",
            "/x\x1b[2J",
            "/a\0b",
            b"/a",
            None,
            1,
            ["/a"],
        ):
            with self.subTest(value=value):
                self.assertFalse(printable_abs(value))


class ModuleRootsTest(Case):
    def test_package_roots_follow_module_files_once_each(self):
        for files, want in (
            (
                ("/installation-one", "/installation-two"),
                ("/installation-one", "/installation-two"),
            ),
            (("/installation", "/installation"), ("/installation",)),
        ):
            with self.subTest(files=files):
                session = ModuleType("ensemble.session")
                session.__file__ = f"{files[0]}/ensemble/session/__init__.py"
                utils = ModuleType("ensemble.utils")
                utils.__file__ = f"{files[1]}/ensemble/utils/__init__.py"
                self.assertEqual(module_roots(session, utils), want)

    def test_a_package_without_a_file_is_rejected(self):
        package = ModuleType("namespace")
        package.__file__ = None
        with self.assertRaisesRegex(ValueError, "package has no source file"):
            module_roots(package)


class Overlaps(Case):
    def test_native_missing_suffixes_respect_filesystem_case_behavior(self):
        root = Path(self.home).resolve()
        parent = root / "parent"
        parent.mkdir()
        insensitive = (root / "PARENT").exists()
        self.assertEqual(overlaps(str(parent / "new"), str(parent / "NEW")), insensitive)
        self.assertEqual(overlaps(str(parent / "new"), str(parent / "NEW" / "child")), insensitive)
        self.assertEqual(overlaps(str(parent / "NEW" / "child"), str(parent / "new")), insensitive)
        self.assertFalse(overlaps(str(parent / "new"), str(parent / "other")))

    def test_missing_suffix_case_matches_the_ancestors_filesystem(self):
        root = Path(self.home).resolve()
        parent = root / "parent"
        parent.mkdir()
        for sensitive in (0, 1):
            with (
                self.subTest(sensitive=sensitive),
                mock.patch("sys.platform", "darwin"),
                mock.patch("os.pathconf", return_value=sensitive) as pathconf,
            ):
                for a, b, expected in (
                    (parent / "new", parent / "NEW", sensitive == 0),
                    (parent / "new", parent / "NEW" / "Child", sensitive == 0),
                    (parent / "NEW" / "Child", parent / "new", sensitive == 0),
                    (parent / "new" / "child", parent / "NEW" / "CHILD", sensitive == 0),
                    (parent / "new", parent / "other", False),
                ):
                    with self.subTest(a=a, b=b):
                        self.assertEqual(overlaps(str(a), str(b)), expected)
                self.assertTrue(pathconf.call_args_list)
                self.assertTrue(
                    all(call.args == (str(parent), 11) for call in pathconf.call_args_list)
                )

    def test_stat_permission_errors_propagate(self):
        error = PermissionError("inaccessible ancestor")
        with (
            mock.patch("os.stat", side_effect=error),
            self.assertRaisesRegex(PermissionError, "^inaccessible ancestor$") as caught,
        ):
            overlaps(os.path.join(self.home, "missing"), self.home)
        self.assertIs(caught.exception, error)

    def test_case_aliases_use_filesystem_identity_even_with_missing_suffixes(self):
        root = Path(self.home).resolve()
        parent = root / "parent"
        parent.mkdir()
        variant = root / "PARENT"
        native_stat = os.stat

        def case_stat(path, *args, **kwargs):
            return native_stat(str(path).replace(str(variant), str(parent), 1), *args, **kwargs)

        with mock.patch("os.stat", side_effect=case_stat):
            for a, b, expected in (
                (parent, variant, True),
                (parent, variant / "missing" / "child", True),
                (variant / "missing" / "child", parent, True),
                (parent / "missing", variant / "missing" / "child", True),
                (parent / "missing", variant / "other", False),
                (parent, root / "parent-other", False),
            ):
                with self.subTest(a=a, b=b):
                    self.assertEqual(overlaps(str(a), str(b)), expected)

    def test_equality_both_directions_siblings_and_missing_suffixes(self):
        root = Path(self.home).resolve()
        parent = root / "parent"
        parent.mkdir()
        alias = root / "alias"
        alias.symlink_to(parent)
        for a, b, expected in (
            (parent, parent, True),
            (parent, parent / "new" / "child", True),
            (parent / "new" / "child", parent, True),
            (parent, root / "parent-other", False),
            (alias / "new", parent / "new" / "child", True),
            (parent / "new-a", parent / "new-b", False),
        ):
            with self.subTest(a=a, b=b):
                self.assertEqual(overlaps(str(a), str(b)), expected)


class ImportEnvironment(Case):
    def setUp(self):
        self.enterContext(
            mock.patch(
                "sysconfig.get_paths",
                return_value={"purelib": "/installation", "platlib": "/installation"},
            )
        )
        self.packages = []
        for name in ("delegate", "session", "utils"):
            module = ModuleType(f"ensemble.{name}")
            module.__file__ = f"/checkout/src/ensemble/{name}/__init__.py"
            self.packages.append(module)
        self.roots = ("/checkout/src",)

    def test_normal_module_and_package_find_the_same_source_root(self):
        module = ModuleType("ensemble.delegate.engine.config")
        module.__file__ = "/checkout/src/ensemble/delegate/engine/config.py"
        module.__spec__ = ModuleSpec(module.__name__, loader=None)
        self.assertEqual(module_roots(module, self.packages[0]), (self.roots[0],))

    def test_import_env_supplies_each_source_root_not_installed_and_on_sys_path(self):
        root = os.path.realpath(next(path for path in sys.path if path))
        everywhere = {"delegate": root, "session": root, "utils": root}
        roots, mixed = os.pathsep.join(self.roots), (root, *self.roots)
        for name, at, installed, pythonpath, want in (
            ("unrelated", {}, "/installation", "/unrelated", {"PYTHONPATH": roots}),
            ("from pythonpath", {}, "/installation", roots, {"PYTHONPATH": roots}),
            ("installed shared", everywhere, root, "/unrelated", {}),
            ("manual bootstrap", everywhere, "/installation", "", {"PYTHONPATH": root}),
            ("mixed", {"delegate": root}, root, "", {"PYTHONPATH": os.pathsep.join(mixed)}),
        ):
            with (
                self.subTest(name=name),
                mock.patch(
                    "sysconfig.get_paths", return_value={"purelib": installed, "platlib": installed}
                ),
                mock.patch.dict(os.environ, {"PYTHONPATH": pythonpath}),
            ):
                for module in self.packages:
                    package = module.__name__.rsplit(".", 1)[-1]
                    module.__file__ = (
                        f"{at[package]}/ensemble/{package}/__init__.py"
                        if package in at
                        else f"/checkout/src/ensemble/{package}/__init__.py"
                    )
                self.assertEqual(import_env(*self.packages), want)


class SourcePath(Case):
    def setUp(self):
        self.installed = os.path.realpath(next(path for path in sys.path if path))

    def test_roots_once_in_order_without_the_installed_ones_on_sys_path(self):
        for roots, installed, want in (
            (
                ("/checkout/src", self.installed, "/repo", "/checkout/src"),
                self.installed,
                {"PYTHONPATH": os.pathsep.join(("/checkout/src", "/repo"))},
            ),
            ((), self.installed, {}),
            ((self.installed,), self.installed, {}),
            (("/off-path",), "/off-path", {"PYTHONPATH": "/off-path"}),
        ):
            with (
                self.subTest(roots=roots, installed=installed),
                mock.patch(
                    "sysconfig.get_paths", return_value={"purelib": installed, "platlib": installed}
                ),
            ):
                self.assertEqual(source_path(roots), want)


class DockerHome(Case):
    def test_a_session_home_under_the_docker_state_root(self):
        with mock.patch.dict(os.environ, {"HOME": "/home/u"}):
            self.assertEqual(docker_home("w", "dir"), "/home/u/.ensemble/docker-state/w/dir")
        self.assertEqual(DOCKER_STATE, "~/.ensemble/docker-state")

    def test_the_home_holding_a_path_under_the_docker_state_root(self):
        with mock.patch.dict(os.environ, {"HOME": "/home/u"}):
            root = "/home/u/.ensemble/docker-state"
            self.assertEqual(docker_home_of(f"{root}/w/dir/p/s.jsonl", "dir"), f"{root}/w/dir")
            self.assertEqual(docker_home_of(f"{root}/w", "dir"), f"{root}/w/dir")
            for path in ("/home/u/.ensemble/docker-statex/w/f", root, "/tmp/f"):
                with self.subTest(path=path):
                    self.assertIsNone(docker_home_of(path, "dir"))


if __name__ == "__main__":
    unittest.main()
