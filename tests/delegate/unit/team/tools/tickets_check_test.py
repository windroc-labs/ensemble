"""tickets_check.py output and exit codes on a small PRD and its ticket descriptions."""

import io
import os
import runpy
import sys
import tempfile
from unittest import mock

from ensemble.delegate.composer.base import TEAM
from tests.delegate.case import Case

SCRIPT = os.path.join(TEAM, "tools", "tickets_check.py")

HELLO = """  @S-1 @FR-1
  Scenario: Hello
    When the user greets
    Then it answers"""
BYE = """  @S-2 @FR-2 @ops
  Scenario: Bye
    When the user leaves
    Then it waves"""
PRD = f"""# Greeter

| ID | Requirement |
|---|---|
| FR-1 | It greets. |
| FR-2 | It parts. |

```gherkin
Feature: Greeter

{HELLO}

{BYE}
```
"""
TOTAL = "3 descriptions, 2 requirements, 2 scenarios"


def ticket(covers, *scenarios, task="build"):
    quoted = "\n\n".join(scenarios)
    return f"Repo: example/app\nCovers: PRD-1 {covers}\nTask: {task}\n\n```gherkin\n{quoted}\n```\n"


TICKETS = {
    "a.md": ticket("FR-1", HELLO),
    "b.md": ticket("FR-2", BYE),
    "v.md": ticket("FR-2", task="verify") + "\nRuns S-2.\n",
}
EDGES = "b.md: a.md\nv.md: b.md\n"


class TicketsCheck(Case):
    def setUp(self):
        super().setUp()
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.prd = os.path.join(root.name, "prd.md")
        self.tickets = os.path.join(root.name, "tickets")
        self.edges = os.path.join(root.name, "edges.txt")
        os.mkdir(self.tickets)
        self.write(self.prd, PRD)

    def write(self, path, text):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def run_script(self, *argv):
        out = io.StringIO()
        with (
            mock.patch.object(sys, "argv", [SCRIPT, *argv]),
            mock.patch.object(sys, "stdout", out),
            mock.patch.object(sys, "stderr", io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            runpy.run_path(SCRIPT, run_name="__main__")
        return raised.exception.code, out.getvalue()

    def check(self, changed=(), edges=EDGES, extra=()):
        for name, text in (TICKETS | dict(changed)).items():
            self.write(os.path.join(self.tickets, name), text)
        self.write(self.edges, edges)
        return self.run_script("check", self.prd, self.tickets, "--edges", self.edges, *extra)

    def test_scenario_prints_the_prd_text_in_the_order_asked(self):
        self.assertEqual(
            self.run_script("scenario", self.prd, "S-2", "S-1"), (0, f"{BYE}\n\n{HELLO}\n")
        )

    def test_scenario_the_prd_lacks(self):
        self.assertEqual(
            self.run_script("scenario", self.prd, "S-1", "S-9"),
            (2, "check failed: no scenario S-9 in the PRD\n"),
        )

    def test_a_consistent_ticket_set_passes(self):
        self.assertEqual(self.check(), (0, f"{TOTAL}: ok\n"))

    def test_a_requirement_split_across_two_tickets(self):
        self.assertEqual(
            self.check({"b.md": ticket("FR-1 to FR-2", BYE)}),
            (
                1,
                f"FR-1: in 2 engineer tickets' Covers: (a.md, b.md), want 1\n{TOTAL}: 1 failures\n",
            ),
        )

    def test_a_requirement_left_to_a_verifier_ticket(self):
        self.assertEqual(
            self.check({"b.md": ticket("FR-7")}),
            (
                1,
                "FR-2: in 0 engineer tickets' Covers: (none), want 1\n"
                "b.md: Covers: names FR-7, not in the PRD's requirement tables\n"
                "S-2: quoted verbatim in 0 engineer tickets (none), want 1\n"
                f"{TOTAL}: 3 failures\n",
            ),
        )

    def test_a_scenario_quoted_in_two_tickets(self):
        self.assertEqual(
            self.check({"b.md": ticket("FR-2", HELLO, BYE)}),
            (
                1,
                "S-1: quoted verbatim in 2 engineer tickets (a.md, b.md), want 1\n"
                f"{TOTAL}: 1 failures\n",
            ),
        )

    def test_a_scenario_with_altered_text_or_in_a_ticket_that_covers_none_of_its_tags(self):
        self.assertEqual(
            self.check(
                {
                    "a.md": ticket("FR-1", HELLO.replace("answers", "replies"), BYE),
                    "b.md": ticket("FR-2"),
                }
            ),
            (
                1,
                "S-1: quoted verbatim in 0 engineer tickets (none), want 1; "
                "tagged but not verbatim in a.md\n"
                "S-2: quoted in a.md, whose Covers: has none of FR-2\n"
                f"{TOTAL}: 2 failures\n",
            ),
        )

    def test_a_dependency_cycle(self):
        self.assertEqual(
            self.check(edges="a.md: b.md\nb.md: a.md\n"),
            (1, f"dependency cycle: a.md -> b.md -> a.md\n{TOTAL}: 1 failures\n"),
        )

    def test_an_edge_to_a_file_outside_the_directory(self):
        self.assertEqual(
            self.check(edges="b.md: a.md c.md\nno edge here\n"),
            (
                1,
                f"edges: c.md is not a description file in the directory\n{TOTAL}: 1 failures\n",
            ),
        )

    def test_a_description_without_its_opening_fields(self):
        self.assertEqual(
            self.check({"v.md": "Repo: example/app\nTask: verify\n"}),
            (1, f"v.md: no Covers: line\n{TOTAL}: 1 failures\n"),
        )

    def test_a_dropped_requirement_and_its_scenario_are_out_of_scope(self):
        self.assertEqual(
            self.check({"b.md": ticket("FR-1")}, extra=("--drop", "FR-2")),
            (
                1,
                "FR-1: in 2 engineer tickets' Covers: (a.md, b.md), want 1\n"
                "3 descriptions, 1 requirements, 2 scenarios: 1 failures\n",
            ),
        )

    def test_without_an_edges_file_no_cycle_is_checked(self):
        for name, text in TICKETS.items():
            self.write(os.path.join(self.tickets, name), text)
        self.assertEqual(self.run_script("check", self.prd, self.tickets), (0, f"{TOTAL}: ok\n"))

    def test_a_check_that_cannot_run_exits_2(self):
        self.assertEqual(
            self.run_script("check", self.prd, self.tickets),
            (2, "check failed: no description files, or no requirement table in the PRD\n"),
        )
        missing = os.path.join(self.tickets, "prd.md")
        self.assertEqual(
            self.run_script("scenario", missing, "S-1"),
            (2, f"check failed: [Errno 2] No such file or directory: '{missing}'\n"),
        )
        self.assertEqual(self.run_script("publish"), (2, "check failed: 2\n"))
