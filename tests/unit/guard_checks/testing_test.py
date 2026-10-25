import os
from unittest import mock

from tests import guards
from tests.case import REPO
from tests.guard_checks import testing
from tests.guard_samples import Sample, fixtures, test_suites


class EveryTest(Sample):
    def test_discovery_finds_direct_and_normalized_test_packages(self):
        root = self.tree(
            {
                "tests/unit/__init__.py": "",
                "tests/project_tests/__init__.py": "",
                "tests/project_tests/integration/__init__.py": "",
                "tests/empty/unit/leftover.py": "",
            }
        )
        self.assertEqual(
            test_suites(root, "tests"),
            [
                (os.path.join(root, "tests/unit"), root),
                (
                    os.path.join(root, "tests/project_tests/integration"),
                    root,
                ),
            ],
        )

    def test_has_no_class_or_module_fixture(self):
        for tests in guards.TESTS:
            self.assertEqual(fixtures(os.path.join(REPO, tests)), [])
        root = self.tree(
            {
                "a.py": "class A:\n    @classmethod\n    def setUpClass(cls): pass\n    @classmethod\n    async def tearDownClass(cls): pass\n",
                "b/c.py": (
                    "def setUpModule(): pass\ndef tearDownModule(): pass\ndef setUp(): pass\n"
                ),
            }
        )
        self.assertEqual(
            fixtures(root),
            [
                "a.py:3: setUpClass",
                "a.py:5: tearDownClass",
                "b/c.py:1: setUpModule",
                "b/c.py:2: tearDownModule",
            ],
        )


class UnitTestModules(Sample):
    def test_each_unit_test_module_needs_its_source_module(self):
        def missing(test, source):
            return [f"{test}: missing source module {source}"]

        for files, expected in (
            (
                {"tests/session/unit/_test.py": "", "src/ensemble/session/__init__.py": ""},
                missing("tests/session/unit/_test.py", "src/ensemble/session/.py"),
            ),
            (
                {
                    path: ""
                    for project, layer in guards.PROJECTS.items()
                    for path in (
                        f"tests/{project}/unit/engine/config_test.py",
                        f"src/ensemble/{layer}/engine/config.py",
                    )
                },
                [],
            ),
            (
                {"tests/session/unit/clients/codex/removed_test.py": ""},
                missing(
                    "tests/session/unit/clients/codex/removed_test.py",
                    "src/ensemble/session/clients/codex/removed.py",
                ),
            ),
            (
                {
                    "tests/orchestrator/unit/engine/router_test.py": "",
                    "src/ensemble/orchestrator/engine/router.py": "",
                },
                [],
            ),
            (
                {
                    "tests/delegate/unit/composer_test.py": "",
                    "src/ensemble/delegate/composer/__init__.py": "",
                },
                [],
            ),
            (
                {
                    "tests/delegate/unit/team/roles_test.py": "",
                    "src/ensemble/delegate/team/roles/engineer.md": "charter",
                },
                [],
            ),
            (
                {"tests/session/unit/fakes/host_test.py": "", "tests/session/fakes/host.py": ""},
                [],
            ),
            (
                {
                    "tests/delegate/unit/composer/support_test.py": "",
                    "tests/delegate/unit/composer/support.py": "",
                },
                missing(
                    "tests/delegate/unit/composer/support_test.py",
                    "src/ensemble/delegate/composer/support.py",
                ),
            ),
            (
                {
                    "tests/session/unit/session_test.py": "",
                    "src/ensemble/session/__init__.py": "",
                },
                [],
            ),
            (
                {
                    "tests/delegate/unit/composer/roles_test.py": "",
                    "src/ensemble/delegate/team/roles/engineer.md": "charter",
                },
                missing(
                    "tests/delegate/unit/composer/roles_test.py",
                    "src/ensemble/delegate/composer/roles.py",
                ),
            ),
            (
                {"tests/session/unit/host_test.py": "", "tests/session/fakes/host.py": ""},
                missing("tests/session/unit/host_test.py", "src/ensemble/session/host.py"),
            ),
            (
                {
                    "tests/utils/unit/case.py": "",
                    "tests/utils/unit/fakes/record.py": "",
                    "tests/utils/unit/live_tmux.py": "",
                    "tests/utils/unit/files_test.py.backup": "",
                    "tests/utils/unit/files_test.txt": "",
                },
                [],
            ),
            (
                {
                    "tests/session/integration/unit/unmatched_test.py": "",
                    "tests/session/session_tests/integration/unmatched_test.py": "",
                    "tests/unit/unmatched_test.py": "",
                    ".github/unit/unmatched_test.py": "",
                },
                [],
            ),
            (
                {"tests/manager/unit/config_test.py": "", "src/ensemble/utils/config.py": ""},
                missing("tests/manager/unit/config_test.py", "src/ensemble/manager/config.py"),
            ),
        ):
            with self.subTest(files=sorted(files)):
                self.assertEqual(testing.unit_test_modules(self.tree(files)), expected)

    def test_empty_directory_does_not_match_a_unit_test(self):
        test = "tests/delegate/unit/team_test.py"
        source = "src/ensemble/delegate/team.py"
        root = self.tree({test: ""})
        os.makedirs(os.path.join(root, "src/ensemble/delegate/team"))
        self.assertEqual(
            testing.unit_test_modules(root), [f"{test}: missing source module {source}"]
        )

    def test_source_package_init_suite_uses_the_package_name(self):
        test = "tests/delegate/unit/composer/__init___test.py"
        root = self.tree({test: "", "src/ensemble/delegate/composer/__init__.py": ""})
        self.assertEqual(len(testing.unit_test_modules(root)), 1)

    def test_missing_modules_are_reported_in_path_order(self):
        root = self.tree(
            {
                "tests/session/unit/z_test.py": "",
                "tests/manager/unit/a_test.py": "",
                "tests/session/unit/a_test.py": "",
            }
        )
        found = testing.unit_test_modules(root)
        self.assertEqual(found, sorted(found))
        self.assertEqual(len(found), 3)


class TestBoundaries(Sample):
    def violations(self, sample, own, *, shipped=False):
        return testing.test_boundaries(
            self.tree(
                {f"src/ensemble/{own}/probe.py" if shipped else f"tests/{own}/probe.py": sample}
            )
        )

    def test_own_module_mutation_patch_aliases_test_imports_and_test_switches_are_rejected(self):
        for sample, own, shipped in (
            *(
                ("from unittest.mock import patch\n" + prefix + call, "session", False)
                for prefix in (
                    "import ensemble.session.engine.hosted_session as module\n",
                    "from ensemble.session.engine import hosted_session as module\n",
                    "import ensemble.session.engine.hosted_session as first\nmodule = first\n",
                )
                for call in (
                    "patch.object(module, 'start')",
                    "module.start = object()",
                    "del module.start",
                    "setattr(module, 'start', object())",
                )
            ),
            *(
                (
                    "from unittest.mock import patch as p\nfrom sys import modules\n" + call,
                    "session",
                    False,
                )
                for call in (
                    "p('ensemble.session.start')",
                    "p('ense' + 'mble.session.start')",
                    "p.dict(modules, {})",
                )
            ),
            *(
                (statement, "session", False)
                for statement in (
                    "import tests.session.unit.api_test as helper",
                    "from tests.session.unit import api_test",
                    "from .api_test import Helper",
                    "importlib.import_module('tests.session.unit.api_test')",
                )
            ),
            *(
                (f"import os\nos.getenv({variable!r})", "session", True)
                for variable in ("CLIENT_TEST", "FORCE_FAILED")
            ),
        ):
            with self.subTest(sample=sample, shipped=shipped):
                self.assertTrue(self.violations(sample, own, shipped=shipped))

    def test_module_table_shipped_patches_and_allowed_fakes_have_exact_diagnostics(self):
        hosted = "from unittest.mock import patch\nimport ensemble.session.engine.hosted_session as hosted\n"
        lower = "from unittest.mock import patch\nfrom ensemble import session\n"
        for sample, own, shipped, expected in (
            *(
                (
                    "from sys import modules\n" + statement,
                    "session",
                    False,
                    ["tests/session/probe.py:2: sys.modules mutation"],
                )
                for statement in (
                    "modules['fake'] = object()",
                    "del modules['fake']",
                    "modules.pop('fake', None)",
                    "modules.update({'fake': object()})",
                    "cleanup(modules.pop, 'fake', None)",
                )
            ),
            *(
                (hosted + statement, "session", False, [f"tests/session/probe.py:3: {reason}"])
                for statement, reason in (
                    (
                        "patch.multiple('ensemble.session.engine.hosted_session', start=object())",
                        "shipped code patch",
                    ),
                    ("patch.dict(hosted.MODES, {})", "shipped dictionary patch"),
                )
            ),
            (
                "from unittest.mock import patch\nimport os\nfrom tests.session import fake_client\npatch.object(os, 'getcwd')\npatch.object(fake_client, 'start')\nfake_client.start = object()\npatch.dict(os.environ, {})\n",
                "delegate",
                False,
                [],
            ),
            *(
                (lower + statement, "manager", False, [f"tests/manager/probe.py:3: {reason}"])
                for statement, reason in (
                    ("patch.object(session, 'sessions')", "shipped object patch"),
                    ("patch('ensemble.session.sessions')", "shipped code patch"),
                    ("patch.multiple(session, sessions=object())", "shipped code patch"),
                    ("patch.dict(session.MODES, {})", "shipped dictionary patch"),
                )
            ),
            ("label = 'TEST_RESULT'", "session", True, []),
        ):
            with self.subTest(sample=sample, shipped=shipped):
                self.assertEqual(self.violations(sample, own, shipped=shipped), expected)

    def test_nested_standard_library_boundaries_and_keyword_patch_targets(self):
        root = self.tree(
            {
                "src/ensemble/session/engine/api.py": "import time\n",
                "tests/session/probe.py": (
                    "from unittest.mock import patch\nfrom ensemble.session.engine import api\npatch.object(api.time, 'sleep')\n"
                ),
            }
        )
        self.assertEqual(testing.test_boundaries(root), [])
        self.assertTrue(
            self.violations(
                "from unittest.mock import patch\npatch(target='ensemble.session.start')", "session"
            )
        )


class UnitIsolation(Sample):
    def test_tests_cannot_patch_or_reach_the_interpreter_gate(self):
        samples = (
            "from unittest import mock\nmock.patch('isolation.Gate.audit')\n",
            "from unittest.mock import patch\npatch.object('isolation.Gate', 'audit')\n",
            "import run_tests as runner\nrunner.GATE.unit = False\n",
            "from run_tests import GATE as gate\ngate.unit = False\n",
            "import run_tests\nrun_tests.isolation.Gate.audit = lambda: None\n",
        )
        for index, text in enumerate(samples):
            with self.subTest(text=text):
                path = f".github/sample{index}_test.py"
                found = testing.test_gate_mutation(self.tree({path: text}))
                self.assertTrue(found, text)
                self.assertTrue(all(line.startswith(path + ":") for line in found), found)

    def test_no_migration_exemption_can_be_reintroduced(self):
        root = self.tree({"tests/session/unit/present_test.py": ""})
        self.assertEqual(testing.unit_isolation_allowlist(root), [])
        with mock.patch.dict(
            guards.UNIT_ISOLATION_ALLOWED, {"tests/session/unit/present_test.py": "escape"}
        ):
            self.assertEqual(
                testing.unit_isolation_allowlist(root),
                [f"{root}/tests/guards.py: unit isolation allowlist must remain empty"],
            )
