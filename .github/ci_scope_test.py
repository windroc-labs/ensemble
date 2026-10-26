"""Contract-derived CI scope, runner filtering and aggregate outcomes."""

import contextlib
import io
import json
import os
import re
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
MANIFESTS = {
    ".claude-plugin/plugin.json": '{\n  "name": "ensemble",\n  "version": "0.3.0",\n  "hooks": "./hooks/hooks.json"\n}\n',
    ".codex-plugin/plugin.json": '{\n  "name": "ensemble",\n  "version": "0.3.0",\n  "interface": {"version": "1"}\n}\n',
    "package.json": '{\n  "name": "ensemble",\n  "version": "0.3.0",\n  "private": true\n}\n',
}
WORKFLOW = Path(scope.__file__).resolve().parent / "workflows/test.yml"


def bumped(text, version="0.4.0"):
    return text.replace('"version": "0.3.0"', f'"version": "{version}"', 1)


class Repository(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.contract(MODULES)
        for module in MODULES:
            (self.root / "src" / module.replace(".", "/")).mkdir(parents=True)

    def contract(self, modules, *, repeat=False):
        text = (
            '[[tool.importlinter.contracts]]\nname = "Production layers"\ntype = "layers"\nlayers = '
            + json.dumps(modules)
            + "\n"
        )
        (self.root / "pyproject.toml").write_text(text * (2 if repeat else 1))

    def test_map_uses_contract_order_and_actual_project_roots(self):
        (self.root / "src/ensemble/manager").rename(self.root / "src/ensemble/control_panel")
        MODULES_RENAMED = ["ensemble.control_panel", *MODULES[1:]]
        self.contract([MODULES[1], MODULES_RENAMED[0], *MODULES[2:]])
        self.assertEqual(
            list(scope.layer_map(self.root).items()),
            list(
                zip(
                    ["orchestrator", "control_panel", *PROJECTS[2:]],
                    [MODULES[1], MODULES_RENAMED[0], *MODULES[2:]],
                    strict=True,
                )
            ),
        )
        result = scope.classify(self.root, ["src/ensemble/control_panel/main.py"], "pull_request")
        self.assertEqual(result["projects"], ["orchestrator", "control_panel"])
        self.assertFalse(result["full"])

    def test_upper_source_docs_skills_and_tests_select_the_entire_upper_group(self):
        for path in (
            "src/ensemble/manager/main.py",
            "hooks/hooks.json",
            "skills/manage/SKILL.md",
            "src/ensemble/orchestrator/main.py",
            "launchd/com.windroc.ensemble.router.plist",
            "skills/board/SKILL.md",
            "tests/manager/unit/main_test.py",
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
            {
                "full": False,
                "docs": True,
                "version": False,
                "broker": False,
                "projects": PROJECTS[:2],
                "reason": "Docs-only changes",
            },
        )
        for paths, event, full in (
            (["README.md", "src/ensemble/session/cli.py"], "pull_request", True),
            (["docs/tests.md", ".github/ci_scope.py"], "pull_request", True),
            (["docs/credentials.md"], "pull_request", True),
            (["README.md", "docs/credentials.md"], "pull_request", True),
            (["skills/act-as/SKILL.md"], "pull_request", True),
            (["skills/manage/SKILL.md"], "pull_request", False),
            (["src/ensemble/delegate/team/roles/engineer.md"], "pull_request", True),
            # Runbook-only PRs keep full scope, so tests/unit/mini_op_runbooks_test.py runs.
            ([".mini-op/runbooks/test-accounts.md"], "pull_request", True),
            (["README.md", "docs/tests.md"], "push", True),
        ):
            with self.subTest(paths=paths, event=event):
                result = scope.classify(self.root, paths, event)
                self.assertFalse(result["docs"])
                self.assertEqual(result["full"], full)

    def git(self, head, base=None):
        """Fake git: the merge-base is "fork", whose blobs are `base`; head's are `head`."""

        def output(args, **_):
            if args[3:] == ["merge-base", "base", "head"]:
                return b"fork\n"
            rev, path = args[4].split(":", 1)
            blobs = {"fork": MANIFESTS if base is None else base, "head": head}.get(rev, {})
            if args[3] != "show" or path not in blobs:
                raise scope.subprocess.CalledProcessError(128, args)
            blob = blobs[path]
            return blob if isinstance(blob, bytes) else blob.encode()

        return mock.patch("subprocess.check_output", side_effect=output)

    def versions(self, paths, edits, base=None):
        with self.git({**MANIFESTS, **edits}, base):
            return scope.classify(self.root, paths, "pull_request", "base", "head")

    def test_version_only_bump_selects_version_scope(self):
        for paths in (
            list(MANIFESTS),
            ["package.json"],
            [".claude-plugin/plugin.json", ".codex-plugin/plugin.json"],
        ):
            with self.subTest(paths=paths):
                self.assertEqual(
                    self.versions(paths, {path: bumped(MANIFESTS[path]) for path in paths}),
                    {
                        "full": False,
                        "docs": False,
                        "version": True,
                        "broker": False,
                        "projects": PROJECTS[:2],
                        "reason": "Version-only changes",
                    },
                )

    def test_more_than_the_version_in_a_version_file_keeps_full(self):
        path = ".claude-plugin/plugin.json"
        text = MANIFESTS[path]
        for name, head, base in (
            ("another field", bumped(text).replace("ensemble", "other"), None),
            ("only formatting", text.replace("\n}", "\n\n}"), None),
            ("version and formatting", bumped(text).replace("\n}", "\n\n}"), None),
            ("respaced version", text.replace('"version": "0.3.0"', '"version":"0.4.0"'), None),
            ("prerelease", bumped(text, "0.4.0-rc.1"), None),
            ("prefixed", bumped(text, "v0.4.0"), None),
            ("number", text.replace('"0.3.0"', "4"), None),
            ("removed", text.replace('  "version": "0.3.0",\n', ""), None),
            ("unchanged text", text, None),
            ("array", "[]\n", None),
            ("invalid", "{\n", None),
            ("deleted", None, None),
            ("added", bumped(text), {}),
            ("non-UTF-8", bumped(text).encode().replace(b"ensemble", b"ens\xffmble"), None),
        ):
            blobs = {**MANIFESTS, path: head} if head is not None else {}
            with self.subTest(name), self.git(blobs, base):
                result = scope.classify(self.root, [path], "pull_request", "base", "head")
                self.assertTrue(result["full"])
                self.assertFalse(result["version"])

    def test_a_nested_version_bump_keeps_full(self):
        path = ".codex-plugin/plugin.json"
        head = bumped(MANIFESTS[path]).replace('{"version": "1"}', '{"version": "2"}')
        self.assertTrue(self.versions([path], {path: head})["full"])

    def test_version_files_with_any_other_path_keep_full(self):
        edits = {path: bumped(text) for path, text in MANIFESTS.items()}
        for other in ("README.md", "docs/tests.md", "src/ensemble/manager/main.py", "src/x.py"):
            with self.subTest(other=other):
                result = self.versions([*MANIFESTS, other], edits)
                self.assertTrue(result["full"])
                self.assertFalse(result["docs"])
                self.assertFalse(result["version"])

    def test_version_scope_needs_both_refs_and_a_pull_request(self):
        edits = {path: bumped(text) for path, text in MANIFESTS.items()}
        with self.git({**MANIFESTS, **edits}):
            for event, refs in (
                ("pull_request", ()),
                ("pull_request", ("base", None)),
                ("push", ("base", "head")),
            ):
                with self.subTest(event=event, refs=refs):
                    result = scope.classify(self.root, list(MANIFESTS), event, *refs)
                    self.assertTrue(result["full"])
                    self.assertFalse(result["version"])

    def test_the_checked_inventory_is_never_docs_only(self):
        self.assertIn(INVENTORY, scope.CHECKED_DOCS)

    def test_broker_scope_for_docs_python_broker_protocol_and_workflow(self):
        for paths, event, broker in (
            (["README.md", "docs/tests.md"], "pull_request", False),
            (["src/ensemble/manager/main.py"], "pull_request", False),
            (["src/ensemble/orchestrator/main.py"], "pull_request", False),
            (["src/ensemble/session/cli.py"], "pull_request", True),
            (["src/broker/main.go"], "pull_request", True),
            (["src/broker/protocol/docker-broker/request.schema.json"], "pull_request", True),
            ([".github/workflows/test.yml"], "pull_request", True),
            (["README.md", "src/broker/go.mod"], "pull_request", True),
            ([], "pull_request", True),
            (["README.md"], "push", True),
            (["manager/a.py"], "workflow_dispatch", True),
        ):
            with self.subTest(paths=paths, event=event):
                self.assertIs(scope.classify(self.root, paths, event)["broker"], broker)

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

    def test_added_upper_layer_comes_from_contract_and_package(self):
        modules = ["ensemble.dashboard", *MODULES]
        self.contract(modules)
        (self.root / "src/ensemble/dashboard").mkdir(parents=True)
        result = scope.classify(self.root, ["src/ensemble/dashboard/cli.py"], "pull_request")
        self.assertFalse(result["full"])
        self.assertEqual(result["projects"], ["dashboard", *PROJECTS[:2]])

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
        (self.root / "src/ensemble/utils").rmdir()
        with self.assertRaises(ValueError):
            scope.layer_map(self.root)

    def test_absent_or_empty_scope_keeps_directories(self):
        directories = ["tests/unit", ".github", "tests/manager/unit"]
        for value in (None, ""):
            with mock.patch.dict(os.environ, {}, clear=True):
                if value is not None:
                    os.environ["TEST_PROJECTS"] = value
                self.assertEqual(scope.selected(self.root, directories), directories)

    def test_partial_scope_filters_by_actual_owner_and_full_keeps_shared(self):
        (self.root / "src/ensemble/manager").rename(self.root / "src/ensemble/renamed")
        self.contract(["ensemble.renamed", *MODULES[1:]])
        directories = [
            "tests/unit",
            ".github",
            "tests/renamed/unit",
            "tests/orchestrator/integration",
            "tests/delegate/unit",
            "tests/renamed_other/unit",
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
                scope.selected(self.root, ["tests/manager/unit"])

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
            'existing=value\nfull=false\ndocs=false\nversion=false\nbroker=false\nprojects=["manager", "orchestrator"]\n',
        )

    def test_cli_pr_diff_of_only_versions_writes_version_scope(self):
        destination = self.root / "outputs"
        edits = {path: bumped(text) for path, text in MANIFESTS.items()}
        with self.git({**MANIFESTS, **edits}) as output:
            diff = "".join(f"{path}\0" for path in MANIFESTS).encode()
            calls = output.side_effect
            output.side_effect = lambda args, **kw: diff if "diff" in args else calls(args, **kw)
            with contextlib.redirect_stdout(io.StringIO()):
                code = scope.main(
                    [
                        "--event",
                        "pull_request",
                        "--base",
                        "base",
                        "--head",
                        "head",
                        "--github-output",
                        str(destination),
                    ],
                    root=self.root,
                )
        self.assertEqual(code, 0)
        self.assertEqual(
            destination.read_text(),
            'full=false\ndocs=false\nversion=true\nbroker=false\nprojects=["manager", "orchestrator"]\n',
        )

    def test_cli_paths_of_only_versions_stay_full(self):
        out = io.StringIO()
        with (
            mock.patch("subprocess.check_output", side_effect=AssertionError("unexpected git")),
            contextlib.redirect_stdout(out),
        ):
            self.assertEqual(
                scope.main(["--event", "pull_request", "--paths", *MANIFESTS], root=self.root), 0
            )
        self.assertTrue(json.loads(out.getvalue())["full"])

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
    def needs(self, *, full=True, event="pull_request", docs=False, version=False):
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
                "mini-op",
            )
        }
        result["changes"]["outputs"] = {
            "full": str(full).lower(),
            "docs": str(docs).lower(),
            "version": str(version).lower(),
            "broker": str(full).lower(),
        }
        result["repository"]["result"] = "success" if full else "skipped"
        result["unit"]["result"] = "skipped" if version else "success"
        coverage = event == "pull_request" and not version
        result["coverage"]["result"] = "success" if coverage else "skipped"
        result["integration"]["result"] = "skipped" if docs or version else "success"
        return result

    def test_expected_full_upper_and_push_results_pass(self):
        for full, event in ((True, "pull_request"), (False, "pull_request"), (True, "push")):
            with self.subTest(full=full, event=event):
                self.assertEqual(scope.gate(self.needs(full=full, event=event), event), [])
        self.assertEqual(scope.gate(self.needs(full=False, docs=True), "pull_request"), [])
        self.assertEqual(scope.gate(self.needs(full=False, version=True), "pull_request"), [])

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
            ("unit", {"full": False, "version": True}),
            ("coverage", {"full": False, "version": True}),
            ("integration", {"full": False, "version": True}),
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

    def test_invalid_or_inconsistent_broker_output_fails(self):
        for value in (None, True, False, "", "TRUE", "unknown"):
            needs = self.needs(full=False)
            needs["changes"]["outputs"]["broker"] = value
            with self.subTest(broker=value):
                self.assertIn(
                    "changes.outputs.broker must be exactly true or false",
                    scope.gate(needs, "pull_request"),
                )
        for full, docs, broker, event in (
            (True, False, "false", "pull_request"),
            (True, False, "false", "push"),
            (False, True, "true", "pull_request"),
        ):
            needs = self.needs(full=full, docs=docs, event=event)
            needs["changes"]["outputs"]["broker"] = broker
            with self.subTest(full=full, docs=docs, broker=broker, event=event):
                self.assertTrue(scope.gate(needs, event))

    def test_invalid_or_inconsistent_version_output_fails(self):
        for value in (None, True, False, "", "TRUE", "unknown"):
            needs = self.needs(full=False, version=True)
            needs["changes"]["outputs"]["version"] = value
            with self.subTest(version=value):
                self.assertIn(
                    "changes.outputs.version must be exactly true or false",
                    scope.gate(needs, "pull_request"),
                )
        for key, value in (("full", "true"), ("docs", "true"), ("broker", "true")):
            needs = self.needs(full=False, version=True)
            needs["changes"]["outputs"][key] = value
            with self.subTest(key=key):
                self.assertTrue(scope.gate(needs, "pull_request"))
        self.assertTrue(scope.gate(self.needs(full=False, version=True, event="push"), "push"))

    def test_broker_check_must_report_success_even_when_go_steps_are_skipped(self):
        for docs, version in ((False, False), (True, False), (False, True)):
            needs = self.needs(full=False, docs=docs, version=version)
            self.assertEqual(scope.gate(needs, "pull_request"), [])
            needs["broker"]["result"] = "skipped"
            self.assertIn(
                "broker: expected success, got 'skipped'", scope.gate(needs, "pull_request")
            )

    def test_mini_op_guard_must_succeed_in_every_scope(self):
        for kwargs in (
            {},
            {"full": False},
            {"full": False, "docs": True},
            {"full": False, "version": True},
            {"event": "push"},
        ):
            needs = self.needs(**kwargs)
            event = kwargs.get("event", "pull_request")
            self.assertEqual(scope.gate(needs, event), [])
            needs.pop("mini-op")
            with self.subTest(**kwargs):
                self.assertIn("mini-op: expected success, got None", scope.gate(needs, event))

    def test_cli_gate_uses_environment_and_reports_diagnostics(self):
        for needs, expected in (
            (self.needs(), 0),
            (self.needs(full=False), 0),
            (self.needs(full=False, version=True), 0),
            ({}, 1),
            (None, 1),
        ):
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


def job(name):
    found = re.search(rf"(?ms)^  {re.escape(name)}:\n(.*?)(?=^  \S|\Z)", WORKFLOW.read_text())
    assert found, f"test.yml has no job {name}"
    return found[1]


class Workflow(unittest.TestCase):
    def test_version_scope_skips_unit_coverage_and_integration_only(self):
        self.assertIn("      version: ${{ steps.scope.outputs.version }}\n", job("changes"))
        for name, condition in (
            ("unit", "needs.changes.outputs.version == 'false'"),
            (
                "coverage",
                "github.event_name == 'pull_request' && needs.changes.outputs.version == 'false'",
            ),
            (
                "integration",
                "needs.changes.outputs.docs == 'false' && needs.changes.outputs.version == 'false'",
            ),
        ):
            with self.subTest(job=name):
                self.assertIn(f"    if: ${{{{ {condition} }}}}\n", job(name))
        for name in ("lint", "broker", "mini-op"):
            with self.subTest(job=name):
                self.assertNotRegex(job(name), r"(?m)^    if:")
