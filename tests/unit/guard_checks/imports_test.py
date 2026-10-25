from pathlib import Path

from tests import guards
from tests.case import REPO
from tests.guard_checks import imports
from tests.guard_data import STANDALONE
from tests.guard_data.orchestrator import ORCH_PKG, ORCH_SESSION_CONTRACT, ORCH_SRC
from tests.guard_data.session import SESSION_SRC, TMUX_HOST
from tests.guard_samples import PKG, Sample


class OrchestratorSessionImports(Sample):
    def check(self, module, sample, config=None):
        root = self.tree(
            {
                "pyproject.toml": config or Path(REPO, "pyproject.toml").read_text(),
                f"{ORCH_SRC}{module}": sample,
            }
        )
        return imports.orchestrator_session_imports(root)

    def message(self, rel, name):
        module = f"{ORCH_PKG}.{rel.removesuffix('.py').replace('/', '.')}"
        return (
            f"{ORCH_SRC}{rel}: {module} imports {name} from "
            "ensemble.session; go through ensemble.delegate"
        )

    def test_only_delegate_imports_pass_however_a_module_imports_the_session(self):
        for module, sample, expected in (
            ("engine/run.py", "from ensemble import delegate\n", []),
            (
                "engine/run_comments.py",
                "from ensemble.session import SessionError\n",
                [self.message("engine/run_comments.py", "SessionError")],
            ),
            *(
                ("engine/promote.py", sample, [self.message("engine/promote.py", "RunLog")])
                for sample in (
                    "from ensemble.session import RunLog\n",
                    "import ensemble.session as s\ns.RunLog()\n",
                    "from ensemble import session\nsession.RunLog()\n",
                    "import ensemble.session\nensemble.session.RunLog.note()\n",
                    "def f():\n    from ensemble.session import RunLog\n",
                )
            ),
        ):
            with self.subTest(module=module, sample=sample):
                self.assertEqual(self.check(module, sample), expected)

    def test_pyproject_ignores_no_imports(self):
        config = Path(REPO, "pyproject.toml").read_text()
        contract = f'name = "{ORCH_SESSION_CONTRACT}"\n'
        self.assertIn(contract, config)
        edge = 'ignore_imports = ["ensemble.orchestrator.engine.prune -> ensemble.session"]\n'
        config = config.replace(contract, contract + edge, 1)
        self.assertEqual(
            self.check("engine/prune.py", "", config),
            [f"pyproject.toml: {ORCH_SESSION_CONTRACT!r} must ignore no imports"],
        )


class OrchestratorImports(Sample):
    """FR-3: orchestrator imports only the standard library, itself, ensemble.utils and the
    names delegate's __all__ exports; no other package imports it.
    """

    DELEGATE = "from ensemble.delegate.engine.base import Delegate\nclass HarnessDelegate: pass\n"
    DELEGATE_INIT = DELEGATE + '__all__ = ["Delegate", "ConfigError"]\n'

    def check(self, files):
        root = self.tree({f"{PKG}/delegate/__init__.py": self.DELEGATE_INIT, **files})
        return imports.orchestrator_imports(root)

    def test_the_real_tree_is_clean(self):
        self.assertEqual(imports.orchestrator_imports(REPO), [])

    def test_only_delegates_exports_utils_itself_and_the_standard_library_cross_the_layer(self):
        rel = f"{ORCH_SRC}engine/prune.py"
        for files, expected in (
            *(
                ({rel: sample}, [])
                for sample in (
                    "from ensemble.delegate import Delegate, ConfigError as DelegateConfigError\n",
                    "import ensemble.delegate as d\nd.Delegate.usage('x')\n",
                    "from ensemble import delegate\ndelegate.ConfigError\n",
                    "from ensemble.utils import Record\nimport ensemble.utils\n",
                    "import os.path\nfrom collections.abc import Mapping\nimport __future__\n",
                    "from ensemble.orchestrator.engine import runs\nfrom . import log\n",
                )
            ),
            *(
                ({rel: sample}, [f"{rel}: imports {name} (FR-3)"])
                for sample, name in (
                    (
                        "from ensemble.delegate import HarnessDelegate\n",
                        "ensemble.delegate.HarnessDelegate",
                    ),
                    ("from ensemble.delegate import composer\n", "ensemble.delegate.composer"),
                    (
                        "from ensemble.delegate.composer import Composer\n",
                        "ensemble.delegate.composer",
                    ),
                    (
                        "import ensemble.delegate as d\nd.composer.Composer()\n",
                        "ensemble.delegate.composer",
                    ),
                    ("import ensemble.delegate.engine.base\n", "ensemble.delegate.engine"),
                    ("from ensemble.session import Session\n", "ensemble.session"),
                    ("from ensemble import manager\n", "ensemble.manager"),
                    ("from ensemble.utils.files import save\n", "ensemble.utils.files"),
                    ("import requests\n", "requests"),
                    (
                        "def f():\n    from ensemble.delegate.engine import harness\n",
                        "ensemble.delegate.engine",
                    ),
                )
            ),
            *(
                ({other: sample}, [f"{other}: imports ensemble.orchestrator (FR-3)"])
                for other in (
                    f"{PKG}/{project}/engine/sample.py"
                    for project in ("manager", "delegate", "session", "utils")
                )
                for sample in (
                    "from ensemble.orchestrator import LinearRun\n",
                    "import ensemble.orchestrator.engine.runs\n",
                    "from ensemble import orchestrator\n",
                    "import ensemble\nensemble.orchestrator.LinearRun\n",
                )
            ),
            (
                {
                    "tests/orchestrator/sample.py": (
                        "from ensemble.delegate.composer import Composer\n"
                    ),
                    "tests/manager/sample.py": "from ensemble import orchestrator\n",
                },
                [],
            ),
        ):
            with self.subTest(files=files):
                self.assertEqual(self.check(files), expected)


class LiteralImports(Sample):
    def test_unknown_contract_types_fail_loudly(self):
        root = self.tree(
            {
                "pyproject.toml": "[[tool.importlinter.contracts]]\ntype = 'unknown'\n",
                "src/ensemble/session/clients/probe.py": "__import__('ensemble.utils')",
            }
        )
        with self.assertRaisesRegex(ValueError, "unsupported import contract type: 'unknown'"):
            imports.literal_imports(root)

    def check(self, sample, project="session", module="clients/probe.py"):
        config = Path(REPO, "pyproject.toml").read_text()
        root = self.tree(
            {
                "pyproject.toml": config,
                f"src/ensemble/{guards.PROJECTS[project]}/{module}": sample,
            }
        )
        return imports.literal_imports(root)

    def test_dynamic_calls_cannot_bypass_unit_contracts(self):
        for sample in (
            "import importlib\nimportlib.import_module('ensemble.session.hosts.tmux.tmux_host')",
            "from importlib import import_module as load\nload('ensemble.session.modes.tui')",
            "__import__('ensemble.manager')",
        ):
            with self.subTest(sample=sample):
                self.assertEqual(len(self.check(sample)), 1)

    def test_lower_public_packages_and_variable_names_pass_but_internals_do_not(self):
        for sample, project, module, expected in (
            (
                "import importlib\nimportlib.import_module('ensemble.utils')\nimportlib.import_module(implementation)",
                "session",
                "clients/probe.py",
                [],
            ),
            ("importlib.import_module('ensemble.session')", "delegate", "engine/probe.py", []),
            (
                "__import__('ensemble.session.engine.hosted_session')",
                "delegate",
                "engine/probe.py",
                [
                    "src/ensemble/delegate/engine/probe.py:1: imports ensemble.session.engine.hosted_session"
                ],
            ),
            (
                "__import__('ensemble.delegate.engine.future')",
                "delegate",
                "engine/config.py",
                [
                    "src/ensemble/delegate/engine/config.py:1: imports ensemble.delegate.engine.future"
                ],
            ),
        ):
            with self.subTest(sample=sample):
                self.assertEqual(self.check(sample, project, module), expected)


class PrivateImports(Sample):
    def test_underscore_names_from_other_modules_but_os_exit_aliases_and_dunders(self):
        for files, expected in (
            (
                {
                    "a.py": "import os as system\nfrom os import _exit\nsystem._exit(0)\n_exit(0)\nsystem._secret()\n"
                },
                ["a.py:5: private system._secret"],
            ),
            (
                {
                    f"{PKG}/a.py": "from b import _x\nfrom . import _y\n",
                    f"{PKG}/c.py": (
                        "import b.d\nfrom e import f as g\n\ndef h():\n    return b.d._z, g._w\n"
                    ),
                },
                [
                    f"{PKG}/a.py:1: private b._x",
                    f"{PKG}/a.py:2: private ._y",
                    f"{PKG}/c.py:5: private b.d._z",
                    f"{PKG}/c.py:5: private g._w",
                ],
            ),
            (
                {
                    f"{PKG}/a.py": "import b as _b\nfrom c import d as _d\n_b.e, _d.f, _b.__file__\n\nclass G:\n    def h(self, i):\n        return self._j, i._k\n"
                },
                [],
            ),
        ):
            with self.subTest(files=sorted(files)):
                self.assertEqual(imports.private_imports(self.tree(files)), expected)


class ImportHygiene(Sample):
    def test_any_sys_path_mutation_and_module_level_environment_write(self):
        assignment = "sys.path[:] = [*roots, *(path for path in sys.path if path not in roots)]"
        script = f"import sys\ndef _bootstrap(roots):\n    {assignment}\ndef main(roots):\n    {assignment}\n"
        for files, expected in (
            *(
                ({path: script}, [f"{path}:3: sys.path mutation", f"{path}:5: sys.path mutation"])
                for path in (f"{SESSION_SRC}modes/tui_hook.py", "tests/other.py")
            ),
            (
                {
                    "tests/a.py": (
                        "import sys\nsys.path.insert(0, 'x')\ndef f():\n    sys.path.append('x')\n"
                    ),
                    "tests/b.py": "import sys\nsys.path = []\nsys.path[0] = 'x'\nsys.path += ['x']\ndel sys.path[0]\n",
                    "tests/c.py": (
                        "import sys as s\nfrom sys import path as p\nfrom unittest import mock\ns.path.extend([])\np.remove('x')\np[:0] = ['x']\np += ['x']\nmock.patch('sys.path', [])\n@mock.patch.object(s, 'path', [])\ndef g(): pass\n"
                    ),
                    "tests/d.py": "import sys\nfrom sys import path\nfrom unittest import mock\nx = [sys.path[0], sys.path.index('x'), list(sys.path), sys.path.copy()]\npath = []\nmock.patch('os.path.join')\nmock.patch.object(sys, 'argv', [])\n",
                },
                [f"{at}: sys.path mutation" for at in ("tests/a.py:2", "tests/a.py:4")]
                + [f"tests/b.py:{n}: sys.path mutation" for n in range(2, 6)]
                + [f"tests/c.py:{n}: sys.path mutation" for n in range(4, 10)],
            ),
            (
                {
                    "tests/a.py": "import os\nos.environ['A'] = '1'\nos.environ.update(A='1')\ndel os.environ['A']\nos.environ |= {}\nos.putenv('A', '1')\n",
                    "tests/b.py": (
                        "import os as o\nfrom os import environ, putenv\nfrom unittest import mock\nclass C:\n    environ.setdefault('A', '1')\nif True:\n    putenv('A', '1')\nmock.patch.dict(environ, {'A': '1'}).start()\no.environ.pop('A')\n"
                    ),
                    "tests/c.py": (
                        "import os\nfrom unittest import mock\nA = os.environ.get('A', os.environ['B'])\ndef f():\n    os.environ['A'] = '1'\n    os.putenv('A', '1')\ng = lambda: os.environ.clear()\n@mock.patch.dict(os.environ, {'A': '1'})\nclass D:\n    @mock.patch.dict(os.environ, {'A': '1'})\n    def h(self): pass\n"
                    ),
                },
                [f"tests/a.py:{n}: module-level environment write" for n in range(2, 7)]
                + [f"tests/b.py:{n}: module-level environment write" for n in (5, 7, 8, 9)],
            ),
        ):
            with self.subTest(files=sorted(files)):
                self.assertEqual(imports.import_hygiene(self.tree(files)), expected)


class StandaloneImports(Sample):
    def test_the_tui_hook_and_tmux_host_use_package_imports(self):
        for path in (f"{SESSION_SRC}modes/tui_hook.py", TMUX_HOST):
            with self.subTest(path=path):
                self.assertNotIn(path, STANDALONE)

    def found(self, source):
        files = dict.fromkeys(STANDALONE, source)
        return imports.standalone_imports(self.tree(files))

    def test_only_stdlib_imports_in_every_branch_of_a_standalone_script(self):
        def reported(*items):
            return sorted(
                f"{path}:{line}: non-stdlib import {name}"
                for path in STANDALONE
                for line, name in items
            )

        for source, expected in (
            (
                "from __future__ import annotations\nimport os.path as path, sys\nfrom collections.abc import Mapping\nimport builtins\ndef load():\n    import json as j\n    from pathlib import Path\n",
                [],
            ),
            (
                "import requests as r, ensemble.utils\nfrom ensemble.session import Driver\nfrom . import sibling\ndef load():\n    from unknown.child import thing\n",
                reported(
                    (1, "requests"),
                    (1, "ensemble.utils"),
                    (2, "ensemble.session"),
                    (3, "."),
                    (5, "unknown.child"),
                ),
            ),
            (
                "if __package__:\n    from ensemble.session.clients import flags\nelse:\n    import runpy\n    import requests\nimport ensemble.utils\n",
                reported((2, "ensemble.session.clients"), (5, "requests"), (6, "ensemble.utils")),
            ),
            (
                "def load():\n    if __package__:\n        import ensemble.utils\nif __package__ is not None:\n    import requests\nif not __package__:\n    import other\n",
                reported((3, "ensemble.utils"), (5, "requests"), (7, "other")),
            ),
        ):
            with self.subTest(source=source):
                self.assertEqual(self.found(source), expected)

    def test_missing_inventory_file_is_reported(self):
        missing = min(STANDALONE)
        root = self.tree({path: "" for path in STANDALONE if path != missing})
        self.assertEqual(
            imports.standalone_imports(root), [f"{missing}: missing standalone script"]
        )

    def test_unlisted_module_is_not_a_standalone_script(self):
        files = dict.fromkeys(STANDALONE, "")
        files[f"{SESSION_SRC}engine/ordinary.py"] = "import ensemble.utils\n"
        self.assertEqual(imports.standalone_imports(self.tree(files)), [])


class OldCore(Sample):
    def test_a_core_import_in_a_tree(self):
        root = self.tree(
            {
                "tests/a.py": "from core.src import drive\n",
                "src/ensemble/session/b.py": "import core\nimport core_x\n",
                ".github/c.py": "import core\n",
                "docs/d.py": "import core\n",
            }
        )
        self.assertEqual(
            imports.old_core(root),
            [
                ".github/c.py: imports core",
                "src/ensemble/session/b.py: imports core",
                "tests/a.py: imports core",
            ],
        )
