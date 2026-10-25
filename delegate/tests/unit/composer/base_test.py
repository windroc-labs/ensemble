"""composer/base.py: its paths and the text helpers ported from the old composer's tests."""

import os
import unittest

from ensemble.delegate.composer import base

from delegate.tests.case import SRC, Case


class Paths(Case):
    def test_team_root_and_no_src(self):
        delegate = os.path.join(SRC["delegate"], "ensemble", "delegate")
        self.assertEqual(base.TEAM, os.path.join(delegate, "team"))
        self.assertTrue(os.path.isfile(os.path.join(base.TEAM, "guide.md")))
        self.assertFalse(hasattr(base, "SRC"))


class Fill(Case):
    def test_a_default_is_used_when_the_value_is_missing_and_a_value_beats_it(self):
        for text, values, want in (
            ("https://{{host|github.com}}/{{repo}}", {"repo": "o/n"}, "https://github.com/o/n"),
            ("{{host|github.com}}", {"host": "ghe.example.com"}, "ghe.example.com"),
        ):
            with self.subTest(values=values):
                self.assertEqual(base.fill(text, values, "t"), want)

    def test_missing_without_default_fails(self):
        with self.assertRaisesRegex(base.ConfigError, r"^unfilled placeholder \{\{repo\}\} in t$"):
            base.fill("{{repo}}", {}, "t")


class Anchor(Case):
    def test_github_style(self):
        self.assertEqual(base.anchor("Light Research"), "light-research")
        self.assertEqual(base.anchor("Template: `x.md`"), "template-xmd")


OUTLINED = """\
# Role

<a id="custom"></a>
## Steps
- **Which build:** x
1. **Done.** y
   * **Nested**: z
text **not** an item
```text
# Fenced
**Fenced item**
```
~~~
## Also fenced
~~~
### Deep
**Item**
## Other
**Other item**
"""


class Outline(Case):
    def test_headings_and_bold_items_outside_fences_with_their_paths_anchors_and_lines(self):
        steps = ("Role", "Steps")
        self.assertEqual(
            base.outline(OUTLINED),
            [
                (0, ("Role",), "role", True),
                (3, steps, "custom", True),
                (4, steps + ("Which build",), "custom", False),
                (5, steps + ("Done",), "custom", False),
                (6, steps + ("Nested",), "custom", False),
                (15, steps + ("Deep",), "deep", True),
                (16, steps + ("Deep", "Item"), "deep", False),
                (17, ("Role", "Other"), "other", True),
                (18, ("Role", "Other", "Other item"), "other", False),
            ],
        )

    def test_a_skipped_level_nests_under_the_last_shallower_heading(self):
        paths = [path for _, path, _, _ in base.outline("# A\n### B\n## C\n### D\n# E\n")]
        self.assertEqual(paths, [("A",), ("A", "B"), ("A", "C"), ("A", "C", "D"), ("E",)])

    def test_only_a_heading_of_one_to_six_hashes_and_a_space_at_the_line_start(self):
        text = "#No space\n####### seven\n # indented\n#\n###### Six\n"
        self.assertEqual(base.outline(text), [(4, ("Six",), "six", True)])

    def test_an_explicit_id_is_the_whole_previous_unfenced_line(self):
        cases = {
            '<a id="x"></a>\n## H': "x",
            'a <a id="x"></a>\n## H': "h",
            '<a id="x"></a>\n\n## H': "h",
            '```\n<a id="x"></a>\n```\n## H': "h",
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual([a for _, _, a, h in base.outline(text) if h], [want])

    def test_a_bold_item_before_any_heading_has_no_anchor(self):
        self.assertEqual(base.outline("**X:** y\n"), [(0, ("X",), "", False)])

    def test_a_bold_item_opens_its_line(self):
        for line in ("**X** y", "- **X**", "* **X.**", "12. **X:**", "  - **X**", "**X**: y"):
            with self.subTest(line=line):
                self.assertEqual(
                    [path for _, path, _, _ in base.outline(f"# T\n{line}\n")][1:], [("T", "X")]
                )
        for line in ("a **X**", "-**X**", "**X", "- a **X**"):
            with self.subTest(line=line):
                self.assertEqual(len(base.outline(f"# T\n{line}\n")), 1)


if __name__ == "__main__":
    unittest.main()
