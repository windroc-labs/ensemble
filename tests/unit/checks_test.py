import importlib
import os
import types

from tests.case import REPO, SRC, Case
from tests.checks import MODULES, checks, discover


class RealTree(Case):
    def test_each_guard_is_clean(self):
        for name, found in checks(REPO):
            with self.subTest(name):
                self.assertEqual(found, [])

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


class Discovery(Case):
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
