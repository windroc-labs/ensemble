"""The orchestrator's own rules are checked without changing shipped source."""

import tomllib
from pathlib import Path

from tests.case import REPO
from tests.guard_checks import linear
from tests.guard_data.orchestrator import ENGINE_CONTRACT, ENGINE_WIRING, ORCH_FILES, ORCH_PKG
from tests.guard_samples import Sample


class EnumComparisons(Sample):
    def test_string_comparisons_in_either_order_and_membership_are_rejected(self):
        for expression in (
            'run.resumable == "yes"',
            '"unknown" != run.resumable',
            'view.status().state == "gone"',
            '"working" is view.state',
            'outcome.status != "needs_input"',
            '"done" == result.outcome.status',
            '"yes" == run.resumable == Resumable.YES',
            'Resumable.NO != run.resumable != "no"',
            'run.state in ("working", "gone")',
            'outcome.status not in {"done", "failed"}',
            'run.resumable in ["yes", Resumable.NO]',
            'run.state in {"gone": 1}',
        ):
            with self.subTest(expression=expression):
                root = self.tree({f"{ORCH_FILES}nested/example.py": expression + "\n"})
                found = linear.linear_enum_comparisons(root)
                self.assertEqual(len(found), 1)
                self.assertTrue(found[0].startswith(f"{ORCH_FILES}nested/example.py:1:"))
                self.assertIn("enum members", found[0])

    def test_enum_none_numeric_and_noncomparison_uses_are_legal(self):
        expressions = (
            "run.resumable == Resumable.YES",
            "run.resumable in (Resumable.YES, Resumable.NO)",
            "view.state is None",
            "outcome.status == OutcomeStatus.DONE",
            "view.state == StatusState.UNKNOWN",
            "response.status == 404",
            'cfg.states["done"] == issue["state"]["id"]',
            'reason == "usage"',
            'log("state", run.state)',
            'text = "run.state == gone"',
            "outcome.status in {}",
            'outcome.status in {None: "done"}',
            'obj.other == "done"',
        )
        root = self.tree({f"{ORCH_FILES}example.py": "\n".join(expressions)})
        self.assertEqual(linear.linear_enum_comparisons(root), [])

    def test_only_linear_source_is_scanned(self):
        root = self.tree(
            {
                "manager/src/ensemble/manager/example.py": 'entry.state == "live"\n',
                "orchestrator/tests/example.py": 'run.state == "working"\n',
            }
        )
        self.assertEqual(linear.linear_enum_comparisons(root), [])

    def test_real_linear_source_uses_enums(self):
        self.assertEqual(linear.linear_enum_comparisons(REPO), [])


ENGINE = f"{ORCH_FILES}engine/"
FLOW = f"{ENGINE}flow/"
TRACKERS = f"{ORCH_FILES}trackers/"


def contract(
    source=f"{ORCH_PKG}.engine",
    forbidden=f"{ORCH_PKG}.trackers",
    name=ENGINE_CONTRACT,
    indirect=None,
    ignore=None,
):
    lines = [
        "[[tool.importlinter.contracts]]",
        f"name = {name!r}",
        'type = "forbidden"',
        f"source_modules = [{source!r}]",
        f"forbidden_modules = [{forbidden!r}]",
    ]
    if indirect is not None:
        lines.append(f"allow_indirect_imports = {str(indirect).lower()}")
    if ignore is not None:
        lines.append(f"ignore_imports = [{ignore!r}]")
    return "\n".join(lines) + "\n"


class EngineTrackerContract(Sample):
    def check(self, config):
        return linear.engine_tracker_contract(self.tree({"pyproject.toml": config}))

    def test_a_contract_forbidding_trackers_from_engine_passes(self):
        self.assertEqual(self.check(contract()), [])

    def test_a_listed_wiring_edge_may_be_ignored(self):
        for edge in sorted(ENGINE_WIRING):
            with self.subTest(edge=edge):
                self.assertEqual(self.check(contract(ignore=edge)), [])

    def test_a_missing_or_weakened_contract_is_reported(self):
        for config, error in (
            (contract(name="other"), f"no contract named {ENGINE_CONTRACT!r}"),
            (
                contract(source=f"{ORCH_PKG}.cli"),
                f"{ENGINE_CONTRACT!r} must have source_modules [{ORCH_PKG}.engine]",
            ),
            (
                contract(forbidden=f"{ORCH_PKG}.trackers.local"),
                f"{ENGINE_CONTRACT!r} must forbid {ORCH_PKG}.trackers",
            ),
            (contract(indirect=True), f"{ENGINE_CONTRACT!r} must not allow indirect imports"),
            (
                contract(ignore=f"{ORCH_PKG}.engine.types -> {ORCH_PKG}.trackers.linear.ops"),
                f"{ENGINE_CONTRACT!r} must not ignore {ORCH_PKG}.engine.types -> {ORCH_PKG}.trackers.linear.ops",
            ),
            (
                contract(ignore=f"{ORCH_PKG}.engine.promote -> {ORCH_PKG}.trackers.linear.client"),
                f"{ENGINE_CONTRACT!r} must not ignore {ORCH_PKG}.engine.promote -> {ORCH_PKG}.trackers.linear.client",
            ),
        ):
            with self.subTest(error=error):
                self.assertEqual(self.check(config), [f"pyproject.toml: {error}"])

    def test_the_real_contract_forbids_trackers_from_engine(self):
        self.assertEqual(linear.engine_tracker_contract(REPO), [])
        contracts = tomllib.loads(Path(REPO, "pyproject.toml").read_text())["tool"]["importlinter"]
        found = next(c for c in contracts["contracts"] if c["name"] == ENGINE_CONTRACT)
        self.assertEqual(found["source_modules"], [f"{ORCH_PKG}.engine"])


class FlowNamesNoLinear(Sample):
    def test_linear_concepts_and_query_text_are_reported(self):
        for line, match in (
            ("project: str", "project"),
            ("    email: str | None", "email"),
            ('"""else its state id."""', "state id"),
            ("# a label ID", "label ID"),
            ("# Linear's keys", "Linear"),
            ("# GraphQL text", "GraphQL"),
            ('Q = "query($i: String!) { x }"', "query($"),
            ('M = "mutation { x }"', "mutation {"),
        ):
            with self.subTest(line=line):
                root = self.tree({f"{FLOW}types.py": f"X = 1\n{line}\n"})
                self.assertEqual(
                    linear.flow_names_no_linear(root),
                    [f"{FLOW}types.py:2: flow names a Linear concept: {match}"],
                )

    def test_package_paths_toml_and_other_trees(self):
        root = self.tree(
            {
                f"{FLOW}workflow.py": 'REL = "orchestrator/src/ensemble/orchestrator/x"\n',
                f"{FLOW}workflow.toml": "# [orchestrator.roles] project\n",
                f"{ENGINE}router.py": "# the Linear project\n",
                f"{TRACKERS}linear/ops.py": 'Q = "query { project { id } }"\n',
            }
        )
        self.assertEqual(
            linear.flow_names_no_linear(root),
            [f"{FLOW}workflow.toml:1: flow names a Linear concept: project"],
        )

    def test_real_flow_names_no_linear_concept(self):
        self.assertEqual(linear.flow_names_no_linear(REPO), [])
