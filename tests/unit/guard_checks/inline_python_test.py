import hashlib

from tests.guard_checks.inline_python import _violations, inline_python
from tests.guard_samples import Sample

PATH = "tests/delegate/example.py"


def key(text):
    return PATH, hashlib.sha256(text.encode()).hexdigest()


class InlinePython(Sample):
    def test_public_package_imports_cannot_resolve_to_internal_modules(self):
        for module, member in (
            ("session", "engine"),
            ("delegate", "engine"),
            ("manager", "cli"),
            ("orchestrator", "engine"),
            ("utils", "record"),
        ):
            with self.subTest(module=module):
                self.assertTrue(_violations(f"from ensemble.{module} import {member} as hidden"))
        self.assertEqual(
            _violations("from ensemble.session import Session\nfrom ensemble.utils import Record"),
            [],
        )

    def test_module_inventory_comes_from_the_scanned_source(self):
        root = self.tree(
            {
                PATH: "argv = ['python3', '-c', 'from ensemble.session import hidden']",
                "src/ensemble/session/__init__.py": "",
                "src/ensemble/session/hidden.py": "",
            }
        )
        self.assertEqual(len(inline_python(root, allowed={})), 1)

    def test_nested_nonlocal_rebinding_cannot_keep_a_literal_exception(self):
        source = (
            "def outer():\n    CODE = 'pass'\n"
            "    def mutate(value):\n        nonlocal CODE\n        CODE = value\n"
            "    return ['python3', '-c', f'{CODE}']\n"
        )
        found = self.checked(source, {key("f\"{'pass'}\""): "Previously literal."})
        self.assertTrue(any("run-time value" in line for line in found), found)
        self.assertTrue(any("stale" in line for line in found), found)

    def test_nonlocal_reads_and_rebinding_another_owner_preserve_outer_literals(self):
        for body in (
            "    def read():\n        nonlocal CODE\n        return CODE\n",
            "    def middle():\n        CODE = 'other'\n        def mutate(value):\n            nonlocal CODE\n            CODE = value\n",
        ):
            source = (
                "def outer():\n    CODE = 'pass'\n" + body + "    return ['python3', '-c', CODE]\n"
            )
            self.assertEqual(self.checked(source), [])

    def test_every_declared_rebinding_invalidates_its_lexical_owner(self):
        for change in (
            "import helper as CODE",
            "from helper import code as CODE",
            "try:\n    pass\nexcept Exception as CODE:\n    pass",
            "match value:\n    case CODE:\n        pass",
            "match value:\n    case [*CODE]:\n        pass",
            "match value:\n    case {'key': _, **CODE}:\n        pass",
            "def CODE(): pass",
            "async def CODE(): pass",
            "class CODE: pass",
        ):
            for declaration in ("global", "nonlocal"):
                with self.subTest(change=change, declaration=declaration):
                    nested = "\n".join("        " + line for line in change.splitlines())
                    body = f"    def mutate():\n        {declaration} CODE\n{nested}\n    mutate()\n    return ['python3', '-c', f'{{CODE}}']\n"
                    source = (
                        "CODE = 'pass'\ndef outer():\n" + body
                        if declaration == "global"
                        else "def outer():\n    CODE = 'pass'\n" + body
                    )
                    found = self.checked(source, {key("f\"{'pass'}\""): "Previously literal."})
                    self.assertTrue(any("run-time value" in line for line in found), found)
                    self.assertTrue(any("stale" in line for line in found), found)

    def test_mutations_and_definitions_invalidate_literal_programs(self):
        for change in (
            "CODE.value = value",
            "CODE[0] = value",
            "del CODE.value",
            "del CODE[0]",
            "del CODE",
            "def CODE(): pass",
            "async def CODE(): pass",
            "class CODE: pass",
        ):
            with self.subTest(change=change):
                source = f"CODE = 'pass'\n{change}\nargv = ['python3', '-c', f'{{CODE}}']"
                found = self.checked(source, {key("f\"{'pass'}\""): "Previously literal."})
                self.assertTrue(any("run-time value" in line for line in found), found)
                self.assertTrue(any("stale" in line for line in found), found)

    def test_nested_attribute_and_subscript_writes_invalidate_the_owner(self):
        for change in ("CODE.value = value", "CODE[0] = value"):
            source = (
                f"CODE = 'pass'\ndef mutate(value):\n    {change}\nargv = ['python3', '-c', CODE]"
            )
            (found,) = self.checked(source)
            self.assertIn("run-time value", found)

    def test_exceptions_match_exact_code_and_stale_entries_fail(self):
        code = "from ensemble.session.modes import tui"
        path = "tests/session/example.py"
        allowed = {
            (
                path,
                hashlib.sha256(code.encode()).hexdigest(),
            ): "Isolates the concrete mode fixture."
        }
        root = self.tree({path: f"import sys\nargv = [sys.executable, '-c', {code!r}]"})
        self.assertEqual(inline_python(root, allowed=allowed), [])
        other = self.tree(
            {
                path: "import sys\nargv = [sys.executable, '-c', 'from ensemble.delegate.engine import plan']"
            }
        )
        self.assertEqual(len(inline_python(other, allowed=allowed)), 2)

    def checked(self, source, allowed=None):
        return inline_python(self.tree({PATH: source}), allowed=allowed or {})

    def test_literals_bindings_and_concatenation_are_checked(self):
        for source in (
            "import sys\nargv = [sys.executable, '-c', 'from ensemble.delegate.engine import plan']",
            "PY = ('python3', '-P')\nCODE = 'from ensemble.delegate' + '.engine import plan'\nargv = (*PY, '-c', CODE)",
            "def run():\n    code = 'from ensemble.session.modes import tui'\n    argv = ['python3', '-c', code]",
            "CODE: str = 'from ensemble.session.modes import tui'\nargv = ['python3', '-c', CODE]",
        ):
            with self.subTest(source=source):
                self.assertEqual(len(self.checked(source)), 1)

    def test_standard_library_and_public_packages_are_allowed(self):
        self.assertEqual(
            _violations(
                "import os, sys\nfrom pathlib import Path\nfrom ensemble.delegate import Delegate\nview = Delegate.attach(sys.argv[1])\nprint(view.content())\nprint(sys._getframe(), view.__class__)\nos._exit(0)"
            ),
            [],
        )

    def test_syntax_nonpublic_imports_and_private_object_access_are_rejected(self):
        for code in (
            "def broken(",
            "from tests.delegate.harness import HarnessCase",
            "from ensemble.session.engine import types",
            "from ensemble.delegate import _private",
            "from . import sibling",
            "import ensemble.delegate as d\nd._private()",
            "from ensemble.delegate import Delegate as D\nview = D.attach('w')\nview._read()",
            "from ensemble.delegate import Delegate\nv: Delegate = Delegate.attach('w')\nv._read()",
            "from ensemble.delegate import Delegate\nv = Delegate.attach('w')\ngetattr(v, '_read')()",
            "import importlib\nimportlib.import_module('ensemble.session.modes')",
        ):
            with self.subTest(code=code):
                self.assertTrue(_violations(code))

    def test_unresolved_programs_are_violations_unless_excepted(self):
        for source in (
            "import sys\nvalue = 1\nargv = [sys.executable, '-c', f'print({value})']",
            "import sys\nfrom tests.delegate.program import CODE\nargv = [sys.executable, '-c', CODE]",
            "import sys\nfrom tests.delegate import program\nargv = [sys.executable, '-c', program.CODE]",
            "import sys\nfor code in ('pass', 'print()'):\n    argv = [sys.executable, '-c', code]",
            "import sys\nCODE = 'pass'\nfor CODE in ('pass',):\n    argv = [sys.executable, '-c', CODE]",
        ):
            with self.subTest(source=source):
                (found,) = self.checked(source)
                self.assertIn(": Python -c: not a literal: ", found)
        source = "import sys\nVALUE = 'print(1)'\nargv = [sys.executable, '-c', f'{VALUE}']"
        allowed = {key("f\"{'print(1)'}\""): "Interpolated literal."}
        self.assertEqual(self.checked(source, allowed), [])
        edited = self.checked(source.replace("print(1)", "print(2)"), allowed)
        self.assertEqual(len(edited), 2)
        self.assertIn("stale Python -c exception", edited[0] + edited[1])

    def test_parameter_programs_cannot_be_excepted(self):
        for source, text in (
            ("import sys\ndef run(code):\n    return [sys.executable, '-c', code]", "code"),
            (
                "PRELUDE = 'import json\\n'\ndef run(code):\n    return ['python3', '-c', PRELUDE + code]",
                "'import json\\n' + code",
            ),
            ("import sys\nrun = lambda code: [sys.executable, '-c', code]", "code"),
            (
                "import sys\ndef run(*codes):\n    for code in codes:\n        argv = [sys.executable, '-c', code]",
                "code",
            ),
            (
                "import sys\ndef run(code):\n    prog = 'pass'\n    prog = code\n    return [sys.executable, '-c', prog]",
                "prog",
            ),
            (
                "import sys\nfrom tests.delegate.program import CODE\nargv = [sys.executable, '-c', CODE]",
                "CODE",
            ),
            (
                "import sys, textwrap\nCODE = 'pass'\nargv = [sys.executable, '-c', textwrap.dedent(CODE)]",
                "textwrap.dedent('pass')",
            ),
            (
                "import sys\ntry:\n    pass\nexcept Exception as c:\n    argv = [sys.executable, '-c', str(c)]",
                "str(c)",
            ),
            (
                "import sys\nmatch sys.argv:\n    case [c]:\n        argv = [sys.executable, '-c', c]",
                "c",
            ),
            ("import sys\nfrom m import *\nargv = [sys.executable, '-c', CODE]", "CODE"),
            (
                "import sys\nCODE = f'{1}'\nif sys.argv:\n    CODE = f'{2}'\nargv = [sys.executable, '-c', CODE]",
                "CODE",
            ),
            (
                "import sys\nCODE = 'x'\ndef f(p):\n    global CODE\n    CODE = p\ndef g():\n    return [sys.executable, '-c', f'{CODE}']",
                "f\"{'x'}\"",
            ),
        ):
            with self.subTest(source=source):
                found = self.checked(source, {key(text): "A helper."})
                self.assertTrue(any("(run-time value" in line for line in found), found)
                self.assertTrue(any("stale" in line for line in found), found)

    def test_same_file_literal_bindings_resolve_and_parameters_do_not(self):
        source = (
            "code = 'from ensemble.session.modes import tui'\n"
            "def run(code):\n"
            "    return ['python3', '-c', code]\n"
        )
        (found,) = self.checked(source)
        self.assertIn("(run-time value", found)
        source = (
            "import sys\n"
            "CODE = 'from ensemble.session.engine import base'\n"
            "class Test:\n"
            "    CODE = 'pass'\n"
            "    def test(self):\n"
            "        return [sys.executable, '-c', CODE]\n"
        )
        self.assertEqual(len(self.checked(source)), 1)

    def test_private_attributes_after_an_ensemble_import_have_no_bypass(self):
        for code in (
            "from ensemble.session import Session\nwith Session.start() as v:\n    v._setup",
            "from ensemble.session import Session\nfor v in [Session]:\n    v._setup",
            "from ensemble.session import Session\n(v := Session)._setup",
            "from ensemble.session import Session\nv, w = Session, 1\nv._setup",
            "import sys\nimport ensemble.session\nsys.modules['ensemble.session'].Session._setup",
            "import importlib\nimportlib.import_module('ensemble.session').Session._setup",
            "import json\nfrom ensemble.session import Session\njson = Session\njson._setup",
            "import sys\nfrom ensemble.session import Session\nsys.S = Session\nsys.S._setup",
            "import sys\nfrom ensemble.session import Session\nhasattr(sys.modules['x'], '_setup')",
            "from ensemble.session import Session\ntry:\n    pass\nexcept Exception as v:\n    v._setup",
            "__import__('ensemble.session').Session._setup",
            "import runpy\nrunpy.run_module('ensemble.session.cli')._setup",
        ):
            with self.subTest(code=code):
                self.assertIn("private attribute _setup", _violations(code))
        self.assertEqual(
            _violations("class Example: pass\nvalue = Example()\nvalue._private = 1"), []
        )
        self.assertEqual(
            _violations(
                "import os, sys\nfrom ensemble.session import Session\ngetattr(os, '_exit')\nhasattr(sys, '_x')"
            ),
            [],
        )

    def test_non_literal_dynamic_imports_and_run_path_are_violations(self):
        for code, error in (
            ("import importlib, sys\nimportlib.import_module(sys.argv[1])", "dynamic import"),
            ("import sys\n__import__(sys.argv[1])", "dynamic import"),
            ("import runpy, sys\nrunpy.run_module(sys.argv[1])", "dynamic import"),
            ("import runpy\nrunpy.run_path('script.py')", "dynamic import"),
            (
                "from importlib import import_module as load\nload('ensemble.session.engine')",
                "import ensemble.session.engine",
            ),
            (
                "import runpy\nrunpy.run_module('ensemble.session.engine.base')",
                "import ensemble.session.engine.base",
            ),
        ):
            with self.subTest(code=code):
                self.assertTrue(any(found.startswith(error) for found in _violations(code)))
        self.assertEqual(_violations("import runpy\nrunpy.run_module('ensemble.session.cli')"), [])

    def test_only_sys_executable_names_the_interpreter(self):
        program = "'from ensemble.session.engine import base'"
        self.assertEqual(self.checked(f"argv = [plan.executable, '-c', {program}]"), [])
        self.assertEqual(
            len(self.checked(f"import sys\nargv = [sys.executable, '-c', {program}]")), 1
        )

    def test_git_config_is_outside_the_rule(self):
        self.assertEqual(self.checked("argv = ['git', '-c', 'user.name=Example']"), [])
