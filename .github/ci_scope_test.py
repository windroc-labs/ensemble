"""Contract-derived CI scope, runner filtering and aggregate outcomes."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ci_scope as scope

from tests.guard_checks.credentials import INVENTORY

MODULES = [
    "ensemble.manager",
    "ensemble.orchestrator",
    "ensemble.delegate",
    "ensemble.session",
    "ensemble.utils",
]
PROJECTS = ["manager", "orchestrator", "delegate", "session", "utils"]


class Repository(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.contract(MODULES)
        for project, module in zip(PROJECTS, MODULES, strict=True):
            (self.root / project / "src" / module.replace(".", "/")).mkdir(parents=True)

    def contract(self, modules, *, repeat=False):
        text = (
            '[[tool.importlinter.contracts]]\nname = "Production layers"\ntype = "layers"\nlayers = '
            + json.dumps(modules)
            + "\n"
        )
        (self.root / "pyproject.toml").write_text(text * (2 if repeat else 1))

    def test_map_uses_contract_order_and_actual_project_roots(self):
        (self.root / "manager").rename(self.root / "control-panel")
        self.contract([MODULES[1], MODULES[0], *MODULES[2:]])
        self.assertEqual(
            list(scope.layer_map(self.root).items()),
            list(
                zip(
                    ["orchestrator", "control-panel", *PROJECTS[2:]],
                    [MODULES[1], MODULES[0], *MODULES[2:]],
                    strict=True,
                )
            ),
        )
        result = scope.classify(self.root, ["control-panel/skills/task/SKILL.md"], "pull_request")
        self.assertEqual(result["projects"], ["orchestrator", "control-panel"])
        self.assertFalse(result["full"])

    def test_upper_source_docs_skills_and_tests_select_the_entire_upper_group(self):
        for path in (
            "manager/src/ensemble/manager/main.py",
            "manager/docs/guide.md",
            "manager/skills/task/SKILL.md",
            "orchestrator/src/ensemble/orchestrator/main.py",
            "orchestrator/docs/guide.md",
            "orchestrator/skills/task/SKILL.md",
            "manager/tests/unit/main_test.py",
        ):
            with self.subTest(path=path):
                result = scope.classify(self.root, [path], "pull_request")
                self.assertEqual(result["projects"], PROJECTS[:2])
                self.assertFalse(result["full"])
                self.assertTrue(result["reason"])

    def test_lower_shared_mixed_unknown_empty_and_non_pr_are_full(self):
        for paths, event in (
            (["delegate/a.py"], "pull_request"),
            (["session/a.py"], "pull_request"),
            (["utils/a.py"], "pull_request"),
            (["tests/a.py"], "pull_request"),
            ([".github/a.py"], "pull_request"),
            (["pyproject.toml"], "pull_request"),
            (["manager/a.py", "utils/a.py"], "pull_request"),
            (["unknown/a.py"], "pull_request"),
            ([], "pull_request"),
            (["manager/a.py"], "push"),
            (["manager/a.py"], "workflow_dispatch"),
        ):
            with self.subTest(paths=paths, event=event):
                result = scope.classify(self.root, paths, event)
                self.assertTrue(result["full"])
                self.assertEqual(result["projects"], PROJECTS)
                self.assertTrue(result["reason"])

    def test_docs_only_pr_selects_docs_scope_and_anything_else_keeps_full(self):
        docs = scope.classify(
            self.root,
            [
                "README.md",
                "CONTEXT.md",
                "docs/tests.md",
                "docs/images/system.svg",
                "docs/readme/README.ja.md",
            ],
            "pull_request",
        )
        self.assertEqual(
            docs,
            {"full": False, "docs": True, "projects": PROJECTS[:2], "reason": "Docs-only changes"},
        )
        for paths, event, full in (
            (["README.md", "session/src/ensemble/session/cli.py"], "pull_request", True),
            (["docs/tests.md", ".github/ci_scope.py"], "pull_request", True),
            (["docs/credentials.md"], "pull_request", True),
            (["README.md", "docs/credentials.md"], "pull_request", True),
            (["delegate/skills/act-as/SKILL.md"], "pull_request", True),
            (["manager/skills/manage/SKILL.md"], "pull_request", False),
            (["delegate/src/ensemble/delegate/team/roles/engineer.md"], "pull_request", True),
            (["README.md", "docs/tests.md"], "push", True),
        ):
            with self.subTest(paths=paths, event=event):
                result = scope.classify(self.root, paths, event)
                self.assertFalse(result["docs"])
                self.assertEqual(result["full"], full)

    def test_the_checked_inventory_is_never_docs_only(self):
        self.assertIn(INVENTORY, scope.CHECKED_DOCS)

    def test_unsafe_or_non_normal_paths_are_full(self):
        for path in (
            "",
            "/manager/a.py",
            "../manager/a.py",
            "manager/../utils/a.py",
            "manager//a.py",
            "./manager/a.py",
            "manager/./a.py",
            "manager/",
            "manager\\a.py",
            "manager/a\x00.py",
        ):
            with self.subTest(path=path):
                self.assertTrue(scope.classify(self.root, [path], "pull_request")["full"])

    def test_missing_duplicate_invalid_or_ambiguous_contracts_fail(self):
        for modules in (
            [],
            [*MODULES, MODULES[0]],
            ["invalid/module", *MODULES[1:]],
            MODULES[:2],
            [1, *MODULES[1:]],
        ):
            self.contract(modules)
            with self.subTest(modules=modules), self.assertRaises(ValueError):
                scope.layer_map(self.root)
        self.contract(MODULES, repeat=True)
        with self.assertRaises(ValueError):
            scope.layer_map(self.root)
        self.contract(MODULES)
        (self.root / "other/src/ensemble/manager").mkdir(parents=True)
        with self.assertRaises(ValueError):
            scope.layer_map(self.root)

    def test_added_upper_layer_comes_from_contract_and_package(self):
        modules = ["ensemble.dashboard", *MODULES]
        self.contract(modules)
        (self.root / "console/src/ensemble/dashboard").mkdir(parents=True)
        result = scope.classify(self.root, ["console/docs/guide.md"], "pull_request")
        self.assertFalse(result["full"])
        self.assertEqual(result["projects"], ["console", *PROJECTS[:2]])

    def test_malformed_contract_structure_fails_explicitly(self):
        for text in (
            '[tool]\nimportlinter = "invalid"\n',
            '[tool.importlinter]\ncontracts = ["invalid"]\n',
            '[tool.importlinter]\ncontracts = "invalid"\n',
        ):
            (self.root / "pyproject.toml").write_text(text)
            with self.subTest(text=text), self.assertRaises(ValueError):
                scope.layer_map(self.root)

    def test_missing_package_or_two_layers_in_one_project_fail(self):
        self.contract([*MODULES, "ensemble.missing"])
        with self.assertRaises(ValueError):
            scope.layer_map(self.root)
        self.contract(MODULES)
        (self.root / "utils/src/ensemble/utils").rename(self.root / "manager/src/ensemble/utils")
        with self.assertRaises(ValueError):
            scope.layer_map(self.root)

    def test_absent_or_empty_scope_keeps_directories(self):
        directories = ["tests/unit", ".github", "manager/tests/unit"]
        for value in (None, ""):
            with mock.patch.dict(os.environ, {}, clear=True):
                if value is not None:
                    os.environ["TEST_PROJECTS"] = value
                self.assertEqual(scope.selected(self.root, directories), directories)

    def test_partial_scope_filters_by_actual_owner_and_full_keeps_shared(self):
        (self.root / "manager").rename(self.root / "renamed")
        directories = [
            "tests/unit",
            ".github",
            "renamed/tests/unit",
            "orchestrator/tests/orchestrator_tests/integration",
            "delegate/tests/unit",
            "renamed-other/tests/unit",
        ]
        with mock.patch.dict(os.environ, {"TEST_PROJECTS": '["renamed", "orchestrator"]'}):
            self.assertEqual(scope.selected(self.root, directories), directories[2:4])
        with mock.patch.dict(os.environ, {"TEST_PROJECTS": json.dumps(["renamed", *PROJECTS[1:]])}):
            self.assertEqual(scope.selected(self.root, directories), directories)

    def test_invalid_scope_fails_explicitly(self):
        for value in (
            " ",
            "not json",
            "null",
            "{}",
            '"manager"',
            "[]",
            '["manager", "manager"]',
            '["unknown"]',
            "[1]",
            '["../manager"]',
        ):
            with (
                self.subTest(value=value),
                mock.patch.dict(os.environ, {"TEST_PROJECTS": value}),
                self.assertRaises(ValueError),
            ):
                scope.selected(self.root, ["manager/tests/unit"])

    def test_changed_paths_preserves_odd_names_and_both_rename_endpoints(self):
        raw = b"manager/old name.py\x00utils/new\nname.py\x00manager/caf\xc3\xa9.py\x00manager/odd\xff.py\x00"
        with mock.patch("subprocess.check_output", return_value=raw) as output:
            self.assertEqual(
                scope.changed_paths(self.root, "base", "head"),
                [os.fsdecode(value) for value in raw.split(b"\x00")[:-1]],
            )
        output.assert_called_once_with(
            [
                "git",
                "-C",
                str(self.root),
                "diff",
                "--name-only",
                "--no-renames",
                "-z",
                "base...head",
            ]
        )
        with mock.patch("subprocess.check_output", return_value=raw):
            self.assertTrue(
                scope.classify(
                    self.root, scope.changed_paths(self.root, "base", "head"), "pull_request"
                )["full"]
            )

    def test_cli_prints_scope_and_appends_workflow_outputs(self):
        destination = self.root / "outputs"
        destination.write_text("existing=value\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = scope.main(
                [
                    "--event",
                    "pull_request",
                    "--paths",
                    "manager/a.py",
                    "--github-output",
                    str(destination),
                ],
                root=self.root,
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["projects"], PROJECTS[:2])
        self.assertEqual(
            destination.read_text(),
            'existing=value\nfull=false\ndocs=false\nprojects=["manager", "orchestrator"]\n',
        )

    def test_cli_pr_uses_diff_but_push_needs_no_diff(self):
        with (
            mock.patch("subprocess.check_output", return_value=b"manager/a.py\x00") as output,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                scope.main(
                    ["--event", "pull_request", "--base", "base", "--head", "head"], root=self.root
                ),
                0,
            )
        self.assertEqual(output.call_count, 1)
        with (
            mock.patch("subprocess.check_output", side_effect=AssertionError("unexpected diff")),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(scope.main(["--event", "push"], root=self.root), 0)

    def test_cli_missing_pr_refs_fails(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertNotEqual(scope.main(["--event", "pull_request"], root=self.root), 0)


class Aggregate(unittest.TestCase):
    def needs(self, *, full=True, event="pull_request", docs=False):
        result = {
            job: {"result": "success"}
            for job in (
                "changes",
                "lint",
                "repository",
                "unit",
                "coverage",
                "integration",
                "broker",
            )
        }
        result["changes"]["outputs"] = {"full": str(full).lower(), "docs": str(docs).lower()}
        result["repository"]["result"] = "success" if full else "skipped"
        result["coverage"]["result"] = "success" if event == "pull_request" else "skipped"
        result["integration"]["result"] = "skipped" if docs else "success"
        return result

    def test_expected_full_upper_and_push_results_pass(self):
        for full, event in ((True, "pull_request"), (False, "pull_request"), (True, "push")):
            with self.subTest(full=full, event=event):
                self.assertEqual(scope.gate(self.needs(full=full, event=event), event), [])
        self.assertEqual(scope.gate(self.needs(full=False, docs=True), "pull_request"), [])

    def test_failure_cancel_and_unexpected_skip_always_fail(self):
        for job in self.needs():
            for outcome in ("failure", "cancelled", "skipped", "unknown"):
                needs = self.needs()
                needs[job]["result"] = outcome
                with self.subTest(job=job, outcome=outcome):
                    failures = scope.gate(needs, "pull_request")
                    self.assertTrue(failures)
                    self.assertIn(job, " ".join(failures))

    def test_expected_skip_is_not_failure_or_cancel_and_unexpected_success_fails(self):
        for job, kwargs in (
            ("repository", {"full": False}),
            ("coverage", {"event": "push"}),
            ("integration", {"full": False, "docs": True}),
        ):
            for result in ("failure", "cancelled", "success"):
                needs = self.needs(**kwargs)
                needs[job]["result"] = result
                with self.subTest(job=job, result=result):
                    self.assertTrue(scope.gate(needs, kwargs.get("event", "pull_request")))

    def test_classifier_failure_or_invalid_output_cannot_authorize_skips(self):
        for value in (None, True, False, "", "False", "TRUE", "unknown"):
            needs = self.needs(full=False)
            needs["changes"]["outputs"]["full"] = value
            with self.subTest(value=value):
                self.assertTrue(scope.gate(needs, "pull_request"))
        for value in (None, True, "", "TRUE", "unknown"):
            needs = self.needs(full=False, docs=True)
            needs["changes"]["outputs"]["docs"] = value
            with self.subTest(docs=value):
                self.assertTrue(scope.gate(needs, "pull_request"))
        self.assertTrue(scope.gate(self.needs(full=True, docs=True), "pull_request"))
        needs = self.needs(full=False)
        needs["changes"]["result"] = "failure"
        self.assertTrue(scope.gate(needs, "pull_request"))

    def test_missing_or_malformed_needs_and_non_full_push_fail(self):
        for needs in ({}, [], {"changes": None}, {**self.needs(), "unit": "success"}):
            with self.subTest(needs=needs):
                self.assertTrue(scope.gate(needs, "pull_request"))
        self.assertTrue(scope.gate(self.needs(full=False, event="push"), "push"))
        self.assertTrue(scope.gate(self.needs(), "workflow_dispatch"))

    def test_cli_gate_uses_environment_and_reports_diagnostics(self):
        for needs, expected in ((self.needs(), 0), (self.needs(full=False), 0), ({}, 1), (None, 1)):
            out = io.StringIO()
            with (
                mock.patch.dict(
                    os.environ, {"CI_NEEDS": json.dumps(needs), "GITHUB_EVENT_NAME": "pull_request"}
                ),
                contextlib.redirect_stdout(out),
            ):
                self.assertEqual(scope.main(["--gate"]), expected)
            self.assertTrue(out.getvalue())
        with (
            mock.patch.dict(
                os.environ, {"CI_NEEDS": "invalid", "GITHUB_EVENT_NAME": "pull_request"}
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(scope.main(["--gate"]), 1)
