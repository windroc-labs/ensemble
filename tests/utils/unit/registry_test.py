"""utils/registry.py: load real fixture files through caller-owned module objects."""

import importlib.util
import os
import unittest
from collections.abc import Sized
from unittest import mock

from ensemble.utils import registry
from tests.utils.case import Case

MODULE = """from collections.abc import Sized


class Ok(Sized):
    def __len__(self):
        return 0


class Half(Sized):
    pass


class Other:
    pass


def function():
    return Ok


VALUE = 1
"""
ABSENT = "ensemble_registry_absent"


class Load(Case):
    def setUp(self):
        self.folder = os.path.join(self.home, "modules")
        os.makedirs(os.path.join(self.folder, "regpkg"))
        self.write("regpkg/__init__.py", "")
        self.write("regpkg/impl.py", MODULE)
        self.write("regbroken.py", "def broken(:\n")
        self.modules = {}
        original_import = importlib.import_module
        files = {"regpkg.impl": "regpkg/impl.py", "regbroken": "regbroken.py"}

        def import_fixture(name, package=None):
            if name not in files:
                return original_import(name, package)
            if name not in self.modules:
                spec = importlib.util.spec_from_file_location(
                    name, os.path.join(self.folder, files[name])
                )
                assert spec is not None and spec.loader is not None
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                self.modules[name] = module
            return self.modules[name]

        self.enterContext(mock.patch.object(importlib, "import_module", side_effect=import_fixture))

    def write(self, name, text):
        with open(os.path.join(self.folder, name), "w", encoding="utf-8") as f:
            f.write(text)

    def refused(self, path, message, base=Sized):
        with self.assertRaises(ValueError) as caught:
            registry.load(path, base)
        self.assertEqual(str(caught.exception), message)

    def test_loads_a_concrete_subclass(self):
        cls = registry.load("regpkg.impl:Ok", Sized)
        self.assertIs(cls, self.modules["regpkg.impl"].Ok)
        self.assertEqual(len(cls()), 0)

    def test_path_syntax(self):
        self.assertTrue(registry.PATH.fullmatch("a_1.b2._c:D_3"))
        for path in (
            "regpkg.impl.Ok",
            "regpkg.impl:",
            ":Ok",
            "regpkg..impl:Ok",
            "regpkg.impl:Ok.x",
            "1pkg:Ok",
            "regpkg/impl:Ok",
            "regpkg.impl:Ok\n",
            "",
            None,
        ):
            with self.subTest(path=path):
                self.refused(path, f"{path}: want module.path:Class")

    def test_a_module_that_cannot_import(self):
        self.refused(
            f"{ABSENT}:Ok", f"{ABSENT}:Ok: cannot import {ABSENT}: No module named '{ABSENT}'"
        )
        with self.assertRaises(ValueError) as caught:
            registry.load("regbroken:Ok", Sized)
        self.assertTrue(
            str(caught.exception).startswith("regbroken:Ok: cannot import regbroken: "),
            caught.exception,
        )
        self.assertIsInstance(caught.exception.__cause__, SyntaxError)

    def test_a_missing_class_or_not_a_concrete_subclass(self):
        for path, message in (
            ("regpkg.impl:Nope", "regpkg.impl:Nope: regpkg.impl has no Nope"),
            *(
                (f"regpkg.impl:{name}", f"regpkg.impl:{name} must be a concrete Sized subclass")
                for name in ("function", "VALUE", "Other", "Sized", "Half")
            ),
        ):
            with self.subTest(path=path):
                self.refused(path, message)


if __name__ == "__main__":
    unittest.main()
