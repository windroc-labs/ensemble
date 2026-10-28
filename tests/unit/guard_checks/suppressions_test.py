from unittest import mock

from tests.case import REPO
from tests.guard_checks import suppressions
from tests.guard_data import NO_COVER_ALLOWED, NOQA_ALLOWED
from tests.guard_samples import Sample

PRAGMA = "# pragma: " + "no cover"


class SourceSuppressions(Sample):
    def scoped(self, scope, comments, *, comment_only=False):
        lines = []
        parts = scope.split(".")
        for depth, name in enumerate(parts):
            declaration = (
                'if __name__ == "__main__":'
                if name == "__main__"
                else f"class {name}:"
                if name[0].isupper()
                else f"def {name}():"
            )
            lines.append("    " * depth + declaration)
        indent = "    " * len(parts)
        lines.extend(indent + ("" if comment_only else "pass  ") + comment for comment in comments)
        if comment_only:
            lines.append(indent + "pass")
        return "\n".join(lines) + "\n"

    def found(self, extra=None, *, replace=None):
        files = {}
        for (path, code, scope), reason in NOQA_ALLOWED.items():
            comments = (
                [f"# pylint: disable={code}  # {reason}", f"# pylint: enable={code}"]
                if code == "duplicate-code"
                else [f"# noqa: {code}  # {reason}"]
            )
            files[path] = files.get(path, "") + self.scoped(scope, comments)
        files.update(extra or {})
        if replace is not None:
            files.pop(replace)
        return suppressions.source_suppressions(self.tree(files))

    def test_the_real_inventory_has_matching_comments(self):
        self.assertEqual(suppressions.source_suppressions(REPO), [])

    def test_each_suppression_needs_its_scopes_inventory_entry_and_reason(self):
        (path, code, scope), reason = next(iter(NOQA_ALLOWED.items()))
        hook = "src/ensemble/session/modes/tui_hook.py"
        hook_comment = f"# noqa: BLE001  # {NOQA_ALLOWED[hook, 'BLE001', 'run']}"
        needs = "suppression needs explicit codes and a recorded reason"
        listed = f"# noqa: {code} # {reason}"
        unmatched = f"{path}: suppression {code} in {scope} has no matching comment"
        e402 = (
            "# noqa: E402",
            "# noqa: F401,E402",
            "# NOQA:I001",
            "# isort: skip_file",
            "# ruff: noqa: E402",
        )
        for files, replace, expected in (
            (
                {hook: self.scoped("run", [hook_comment]) + self.scoped("other", [hook_comment])},
                None,
                [f"{hook}:4: suppression BLE001 in other is not allowed"],
            ),
            (
                {
                    "tests/other.py": self.scoped(
                        "other",
                        [
                            "# pylint: disable=duplicate-code  # not approved",
                            "# pylint: enable=duplicate-code",
                        ],
                    )
                },
                None,
                [
                    "tests/other.py:2: suppression duplicate-code in other is not allowed",
                    "tests/other.py:3: suppression duplicate-code in other has no matching disable",
                ],
            ),
            (
                {
                    "other/unused.py": "# noqa\n",
                    "src/ensemble/utils/strings.py": (
                        '"""Example: # noqa and # type: ignore."""\nvalue = \'# noqa: F401\'\n# Suppressions are checked elsewhere.\n'
                    ),
                },
                None,
                [],
            ),
            *(
                ({where: f"value = 1  {comment}\n"}, None, [f"{where}:1: {needs}"])
                for where, comment in (
                    *(
                        ("src/ensemble/utils/a.py", comment)
                        for comment in ("# noqa", "# noqa:", "# noqa: F401", "# noqa: BLE001")
                    ),
                    *(
                        ("src/ensemble/delegate/a.py", comment)
                        for comment in (
                            "# type: ignore",
                            "# type: ignore[assignment]",
                            "#type:ignore",
                        )
                    ),
                    *(
                        (where, comment)
                        for where in (
                            "src/ensemble/session/sample.py",
                            "tests/session/unit/sample_test.py",
                            "tests/unit/sample_test.py",
                            ".github/sample.py",
                        )
                        for comment in (
                            "# ruff: noqa",
                            "# ruff: noqa: E402",
                            "# flake8: noqa",
                            "# noqa: E402, I001",
                            "# type: ignore",
                            "# pyright: ignore",
                            "# pylint: disable=all",
                            "# isort: skip",
                        )
                    ),
                )
            ),
            (
                {"tests/a.py": "\n".join(f"import x  {comment}" for comment in e402) + "\n"},
                None,
                [f"tests/a.py:{line}: {needs}" for line in range(1, 6)],
            ),
            (
                {"src/ensemble/utils/a.py": "value = 1  # noqa: BLE001  # Forward failures.\n"},
                None,
                ["src/ensemble/utils/a.py:1: suppression BLE001 in <module> is not allowed"],
            ),
            (
                {path: self.scoped(scope, [f"# noqa: {code}, F401  # {reason}"])},
                None,
                [f"{path}:3: suppression F401 in {scope} is not allowed"],
            ),
            (
                {path: self.scoped(scope, [f"# noqa: {code}  # Different reason."])},
                None,
                [
                    f"{path}:3: suppression {code} in {scope} reason differs from NOQA_ALLOWED",
                    unmatched,
                ],
            ),
            ({path: "value = 1\n"}, None, [unmatched]),
            ({}, path, [unmatched]),
            (
                {path: self.scoped(scope, [f"# noqa: {code}  # {reason}"] * 2)},
                None,
                [f"{path}:4: duplicate suppression {code} in {scope}"],
            ),
            (
                {
                    path: self.scoped(
                        scope, [f"#  NOQA : {code.lower()} # {reason}"], comment_only=True
                    )
                },
                None,
                [],
            ),
            (
                {path: self.scoped(scope, [f"# noqa: {code} # {reason} # type: ignore"])},
                None,
                [
                    f"{path}:3: suppression {code} in {scope} reason differs from NOQA_ALLOWED",
                    unmatched,
                ],
            ),
            *(
                (
                    {path: self.scoped(scope, [listed]) + self.scoped(other, [listed])},
                    None,
                    [
                        f"{path}:{len(scope.split('.')) + len(other.split('.')) + 2}: "
                        f"suppression {code} in {other} is not allowed"
                    ],
                )
                for other in (
                    "other",
                    "Other._pump",
                    "HeadlessMode.other",
                    "HeadlessMode._pump.nested",
                )
            ),
            ({}, None, []),
            (
                {path: self.scoped(scope + ".nested", [listed])},
                None,
                [f"{path}:4: suppression {code} in {scope}.nested is not allowed", unmatched],
            ),
            (
                {path: self.scoped(scope, [listed]).replace("def _pump", "async def _pump")},
                None,
                [],
            ),
            (
                {path: f"pass  {listed}\n"},
                None,
                [f"{path}:1: suppression {code} in <module> is not allowed", unmatched],
            ),
        ):
            with self.subTest(files=files, replace=replace):
                self.assertEqual(self.found(files, replace=replace), expected)

    def test_a_pylint_disable_needs_a_matching_enable_in_its_scope(self):
        path = "tests/example.py"
        reason = "Fixture approval."
        disable = self.scoped("outer", [f"# pylint: disable=duplicate-code  # {reason}"])
        outer = f"{path}: suppression duplicate-code in outer has no matching enable"
        with mock.patch.dict(NOQA_ALLOWED, {(path, "duplicate-code", "outer"): reason}):
            for source, expected in (
                (disable, [outer]),
                (
                    disable + self.scoped("other", ["# pylint: enable=duplicate-code"]),
                    [
                        f"{path}:4: suppression duplicate-code in other has no matching disable",
                        outer,
                    ],
                ),
            ):
                with self.subTest(source=source):
                    self.assertEqual(self.found({path: source}), expected)


class CoveragePragmas(Sample):
    def found(self, files, allowed=None):
        with mock.patch.dict(NO_COVER_ALLOWED, allowed or {}, clear=True):
            return suppressions.coverage_pragmas(self.tree(files))

    def test_the_real_inventory_has_matching_comments(self):
        self.assertEqual(suppressions.coverage_pragmas(REPO), [])

    def test_a_registered_function_header_may_carry_the_pragma(self):
        path = "src/ensemble/utils/a.py"
        for scope, source in (
            ("run", f"def run():  {PRAGMA}\n    pass\n"),
            ("run", f"def run(\n    a,  {PRAGMA}\n):\n    pass\n"),
            ("run", f"def run(\n    a: int,\n) -> dict[str, int]:  {PRAGMA}\n    pass\n"),
            ("Host.start", f"class Host:\n    async def start(self):  {PRAGMA}\n        pass\n"),
            ("run.inner", f"def run():\n    def inner():  {PRAGMA}\n        pass\n"),
        ):
            with self.subTest(source=source):
                self.assertEqual(self.found({path: source}, {(path, scope): "Spawns."}), [])

    def test_an_unregistered_function_is_rejected(self):
        path = "src/ensemble/utils/a.py"
        source = f"def run():  {PRAGMA}\n    pass\n\n\ndef other():  {PRAGMA}\n    pass\n"
        self.assertEqual(
            self.found({path: source}, {(path, "run"): "Spawns."}),
            [f"{path}:5: pragma no cover in other is not allowed"],
        )

    def test_a_pragma_outside_a_def_header_is_rejected_whatever_its_spelling(self):
        path = "src/ensemble/utils/a.py"
        for line, source in (
            (1, f"value = 1  {PRAGMA}\n"),
            (2, f"def run():\n    pass  {PRAGMA}\n"),
            (2, "def run():\n    pass  #pragma:" + "no cover\n"),
            (2, "def run():\n    pass  # PRAGMA " + "NO COVER\n"),
            (1, f"text = '{PRAGMA}'\n"),
            (1, f"class Host:  {PRAGMA}\n    pass\n"),
        ):
            with self.subTest(source=source):
                self.assertEqual(
                    self.found({path: source}, {(path, "run"): "Spawns."}),
                    [f"{path}:{line}: pragma no cover must be on a def line"]
                    + [f"{path}: pragma no cover in run has no matching comment"],
                )

    def test_a_second_pragma_in_one_function_is_a_duplicate(self):
        path = "src/ensemble/utils/a.py"
        source = f"def run(  {PRAGMA}\n    a,  {PRAGMA}\n):\n    pass\n"
        self.assertEqual(
            self.found({path: source}, {(path, "run"): "Spawns."}),
            [f"{path}:2: duplicate pragma no cover in run"],
        )

    def test_an_entry_needs_a_reason(self):
        path = "src/ensemble/utils/a.py"
        self.assertEqual(
            self.found({path: f"def run():  {PRAGMA}\n    pass\n"}, {(path, "run"): " "}),
            [f"{path}:1: pragma no cover in run needs a reason"],
        )

    def test_each_entry_needs_a_pragma(self):
        path = "src/ensemble/utils/a.py"
        for files in ({path: "def run():\n    pass\n"}, {}):
            with self.subTest(files=files):
                self.assertEqual(
                    self.found(files, {(path, "run"): "Spawns."}),
                    [f"{path}: pragma no cover in run has no matching comment"],
                )
