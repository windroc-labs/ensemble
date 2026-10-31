from tests.guard_checks.comments import internal_ids
from tests.guard_samples import Sample

PY = "src/ensemble/sample.py"


class InternalIds(Sample):
    def found(self, files):
        return internal_ids(self.tree(files))

    def test_a_python_comment_or_docstring_citing_an_id_is_flagged(self):
        text = (
            '"""Module.\n\nIts rule (WDRC-109 FR-52)."""\n'
            "X = 1  # a bound (NFR-2)\n"
            "def f():\n"
            '    """AC9 (D5): a test description; Q15."""\n'
        )
        self.assertEqual(
            [line.split(": state")[0] for line in self.found({PY: text})],
            [
                f"{PY}:3: FR-52",
                f"{PY}:3: WDRC-109",
                f"{PY}:4: NFR-2",
                f"{PY}:6: AC9",
                f"{PY}:6: D5",
                f"{PY}:6: Q15",
            ],
        )

    def test_the_message_says_to_state_the_rule(self):
        self.assertEqual(
            self.found({PY: "# see WDRC-1\n"}),
            [f"{PY}:1: WDRC-1: state the rule it stands for instead of an internal id"],
        )

    def test_ids_in_string_literals_are_data(self):
        text = (
            'ISSUE = "WDRC-241"\n'
            'BRANCH = f"engineer-{ISSUE}-FR-3"\n'
            'HELP = "e.g. TASK-123 or WDRC-7"\n'
            "def f(ref='FR-1'):\n"
            '    return run(ref, "AC1")\n'
        )
        self.assertEqual(self.found({PY: text}), [])

    def test_other_file_kinds_check_their_comments_only(self):
        files = {
            "src/broker/a.go": 'x := "WDRC-1"\n// a rule (FR-52)\ny := 2 // AC3\n',
            "src/broker/s.json": '{"$comment": "a name (WDRC-109)", "id": "WDRC-2"}\n',
            ".github/workflows/w.yml": "run: echo WDRC-3  # FR-4\n",
            "scripts/x.sh": 'echo "#WDRC-5"\n# NFR-1\n',
            "src/ensemble/config.toml": 'key = "WDRC-6"\n',
            "skills/x/SKILL.md": "# FR-1 is a markdown heading\n",
        }
        self.assertEqual(
            [line.split(": state")[0] for line in self.found(files)],
            [
                ".github/workflows/w.yml:1: FR-4",
                "scripts/x.sh:2: NFR-1",
                "src/broker/a.go:2: FR-52",
                "src/broker/a.go:3: AC3",
                "src/broker/s.json:1: WDRC-109",
            ],
        )

    def test_a_longer_token_is_not_an_id(self):
        text = "# UTF-8, SHA-256, x-FR-1, FR-1x, AC, D, Q1, QA12, 0xD1, AC-1\n"
        self.assertEqual(self.found({PY: text}), [])
