import importlib
import os
import types
import unittest

from tests.case import REPO, SRC, Case
from tests.checks import MODULES, checks, discover


class RealTree(Case):
    def test_src_modules_load_from_src(self):
        for root in (r for sub, r in SRC.items() if sub != "orchestrator"):
            src = os.path.realpath(root)
            for top, dirs, files in os.walk(root):
                dirs[:] = sorted(d for d in dirs if d != "__pycache__")
                for name in sorted(f for f in files if f.endswith(".py")):
                    parts = os.path.relpath(os.path.join(top, name), root)[:-3].split(os.sep)
                    parts = parts[:-1] if parts[-1] == "__init__" else parts
                    module = importlib.import_module(".".join(parts))
                    real = os.path.realpath(module.__file__)
                    self.assertEqual(os.path.commonpath([src, real]), src)


def _guard_test(name):
    def test(self):
        self.assertEqual(list(checks(REPO, names={name})), [(name, [])])

    return test


for _guard_name, _guard in discover():
    setattr(RealTree, f"test_guard_{_guard_name}", _guard_test(_guard_name))


class Discovery(Case):
    def test_selected_checks_keep_root_rules_and_reject_unknown_names(self):
        module = types.ModuleType(__name__)
        module.first = lambda root: [root]
        module.private_imports = lambda root: [root]
        self.assertEqual(
            list(checks("/repo", names={"private_imports"}, modules=(module,))),
            [("private_imports", ["/repo/src"])],
        )
        self.assertEqual(
            list(checks("/repo", names={"first"}, modules=(module,))), [("first", ["/repo"])]
        )
        self.assertEqual(
            list(checks("/repo", modules=(module,))),
            [("first", ["/repo"]), ("private_imports", ["/repo/src"])],
        )
        with self.assertRaisesRegex(ValueError, "unknown guard"):
            list(checks("/repo", names={"missing"}, modules=(module,)))

    def test_each_discovered_guard_has_exactly_one_schedulable_test(self):
        names = unittest.defaultTestLoader.getTestCaseNames(RealTree)
        self.assertEqual(
            sorted(
                name.removeprefix("test_guard_") for name in names if name.startswith("test_guard_")
            ),
            [name for name, _ in discover()],
        )

    def test_every_concern_module_contributes_its_public_functions_once_by_name(self):
        found = discover()
        names = [name for name, _ in found]
        self.assertEqual(names, sorted(set(names)))
        self.assertEqual([name for name in names if name.startswith("_")], [])
        self.assertEqual(
            {check.__module__ for _, check in found},
            {module.__name__ for module in MODULES} - {"tests.guard_checks.common"},
        )

    def test_imported_and_private_functions_are_not_checks(self):
        module = types.ModuleType("sample")
        exec(
            "from os.path import join\ndef _helper(repo):\n    return []\n"
            "def check(repo):\n    return []\n",
            vars(module),
        )
        self.assertEqual([name for name, _ in discover((module,))], ["check"])

    def test_a_name_in_two_modules_raises(self):
        first, second = types.ModuleType("first"), types.ModuleType("second")
        for module in (first, second):
            exec("def same(repo):\n    return []\n", vars(module))
        with self.assertRaisesRegex(ValueError, r"^same: in first and second$"):
            discover((first, second))
