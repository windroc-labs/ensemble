"""scope_check.py verdicts on decompose tickets' Scope: lists."""

import io
import os
import runpy
import sys
from pathlib import Path
from unittest import mock

from ensemble.delegate.composer.base import TEAM
from tests.delegate.case import Case
from tests.delegate.team_tools import tool

SCRIPT = os.path.join(TEAM, "tools", "scope_check.py")
scope_check = tool("scope_check")

REPO = "acme/app"
FILES = ("src/app/main.py", "src/app/util.py", "config.toml", "docs/readme/README.fr.md")
PRD = "| ID | 需求 | 验收 |\n| -- | -- | -- |\n| FR-1 | a | b |\n| NFR-1 | c | d |\n"


def ticket(scope, *, covers="FR-1, NFR-1", task="build", repo=REPO):
    lines = [f"Repo: {repo}", f"Covers: PRD-1 {covers}", f"Task: {task}", ""]
    if scope is not None:
        lines += ["Scope:", *(f"- {item}" for item in scope), ""]
    return "\n".join(lines + ["## What to build", "", "Things."])


class ScopeCheck(Case):
    def setUp(self):
        super().setUp()
        self.tree = Path(self.home, "tree")
        for rel in FILES:
            Path(self.tree, rel).parent.mkdir(parents=True, exist_ok=True)
            Path(self.tree, rel).write_text("x", encoding="utf-8")

    def check(self, *tickets, prd=PRD, bases=None):
        prd_path = Path(self.home, "prd.md")
        prd_path.write_text(prd, encoding="utf-8")
        paths = []
        for n, text in enumerate(tickets, 1):
            path = Path(self.home, f"t{n}.md")
            path.write_text(text, encoding="utf-8")
            paths.append(str(path))
        args = []
        for base in bases if bases is not None else [f"{REPO}={self.tree}"]:
            args += ["--base", base]
        out = io.StringIO()
        code = scope_check.main([*args, str(prd_path), *paths], out=out)
        return code, out.getvalue()

    def failures(self, *tickets, **kwargs):
        code, out = self.check(*tickets, **kwargs)
        self.assertEqual(code, 1, out)
        return out

    def test_disjoint_existing_and_new_scopes_pass(self):
        one = ticket(["`src/app/main.py`", "`src/app/new.py` (new)", "`config.toml` `[a] x`"])
        two = ticket(
            ["`src/app/util.py`", "`config.toml` `[a] y`", "`docs/readme/`"],
            covers="",
            task="light-build",
        )
        verify = ticket(None, task="verify")
        self.assertEqual(self.check(one, two, verify), (0, "ok\n"))

    def test_each_scope_path_exists_or_is_marked_new(self):
        out = self.failures(ticket(["`src/app/gone.py`", "`src/*/none.*`"]))
        self.assertIn("t1.md: Scope `src/app/gone.py` not on acme/app's base", out)
        self.assertIn("t1.md: Scope `src/*/none.*` not on acme/app's base", out)
        self.assertEqual(self.check(ticket(["`src/*/main.py`"])), (0, "ok\n"))
        for outside in ("`/etc/hosts`", "`../tree/config.toml`"):
            with self.subTest(outside=outside):
                self.assertIn(f"Scope {outside} not on", self.failures(ticket([outside])))

    def test_scopes_are_disjoint_across_tickets(self):
        overlapping = (
            ("`src/app/main.py`", "`src/app/main.py`"),
            ("`src/app/`", "`src/app/util.py`"),
            ("`src/*/main.py`", "`src/app/main.py`"),
            ("`config.toml`", "`config.toml` `[a] x`"),
            ("`config.toml` `[a] x`", "`config.toml` `[a] x`"),
            ("`src/app/new/` (new)", "`src/app/new/a.py` (new)"),
            ("`src/app/main.py` rename `foo`", "`src/app/main.py` add `bar`"),
        )
        for left, right in overlapping:
            with self.subTest(left=left, right=right):
                out = self.failures(ticket([left]), ticket([right], covers=""))
                self.assertIn("t1.md and t2.md: Scope overlaps on ", out)
        self.assertEqual(
            self.check(
                ticket(["`src/app/main.py`"]), ticket(["`src/app/main.pyc` (new)"], covers="")
            ),
            (0, "ok\n"),
        )
        other = ticket(["`src/app/main.py`"], covers="", repo="acme/other")
        bases = [f"{REPO}={self.tree}", f"acme/other={self.tree}"]
        self.assertEqual(self.check(ticket(["`src/app/main.py`"]), other, bases=bases), (0, "ok\n"))

    def test_prd_paths_and_keys_have_exactly_one_owner(self):
        prd = (
            PRD + "Change `src/app/main.py`, add `src/app/new.py`, set `[a] x` in `config.toml`.\n"
        )
        out = self.failures(ticket(["`src/app/util.py`"]), prd=prd + "Read `.hooks/new.js`.\n")
        self.assertIn("PRD names `.hooks/new.js`: owned by 0 tickets", out)
        for literal in ("src/app/main.py", "src/app/new.py", "[a] x", "config.toml"):
            self.assertIn(f"PRD names `{literal}`: owned by 0 tickets", out)
        out = self.failures(
            ticket(["`src/app/main.py`", "`config.toml` `[a] x`"]),
            ticket(["`src/app/new.py` (new)", "`config.toml` `[a] y`"], covers=""),
            prd=prd,
        )
        self.assertIn("PRD names `config.toml`: owned by 2 tickets (t1.md, t2.md)", out)
        self.assertNotIn("`[a] x`", out)

    def test_non_path_literals_in_the_prd_are_ignored(self):
        prd = PRD + " ".join(
            f"`{text}`"
            for text in (
                "/clear",
                "/ensemble:manage",
                "~/.ensemble/managers/",
                "roster.json",
                "ensemble list </dev/null",
                "--manager <m>",
                "kill -9",
                "acme/app",
                "rearm.py --mode manual",
                "os.listdir",
                "*",
                "[0-9]",
                "",
            )
        )
        self.assertEqual(self.check(ticket(["`src/app/main.py`"]), prd=prd), (0, "ok\n"))

    def test_every_requirement_has_one_owning_engineer_ticket(self):
        out = self.failures(
            ticket(["`src/app/main.py`"], covers="FR-1"),
            ticket(["`src/app/util.py`"], covers="FR-1", task="light-build"),
            ticket(None, covers="FR-1, NFR-1", task="verify"),
        )
        self.assertIn("FR-1: owned by 2 engineer tickets (t1.md, t2.md)", out)
        self.assertIn("NFR-1: owned by 0 engineer tickets", out)

    def test_engineer_ticket_needs_a_scope_and_a_known_repo(self):
        out = self.failures(ticket(None), ticket(["`src/app/util.py`"], covers="", repo="acme/x"))
        self.assertIn("t1.md: no Scope:", out)
        self.assertIn("t1.md: no Scope:", self.failures(ticket([]), ticket(None, task="verify")))
        bare = self.failures(ticket(["`src/app/main.py`", "src/app/util.py"]))
        self.assertIn("t1.md: Scope item `- src/app/util.py` names no `path`", bare)
        self.assertIn("t2.md: Repo: acme/x has no --base", out)

    def test_linear_saved_description_keeps_its_scope(self):
        # Linear saves a blank line after `Scope:` and `* ` bullets
        saved = (
            f"Repo: {REPO}\nCovers: PRD-1 FR-1, NFR-1\nTask: build\n\nScope:\n\n"
            "* `src/app/main.py`\n* `config.toml` `[a] x`（不改）\n\n## Parent\n\nPRD-1.\n\n"
            "* not a path\n"
        )
        self.assertEqual(self.check(saved), (0, "ok\n"))
        for after in ("\n\n", "\n\n## Parent\n\n* `src/app/main.py`\n"):
            with self.subTest(after=after):
                head = saved.split("Scope:", 1)[0]
                self.assertIn("t1.md: no Scope:", self.failures(head + "Scope:" + after))

    def test_usage_and_unreadable_files_exit_2(self):
        out = io.StringIO()
        self.assertEqual(scope_check.main([], out=out), 2)
        self.assertIn("check failed: usage:", out.getvalue())
        for bad in (["--base", "no-equals"], ["--base", f"{REPO}={self.home}/missing"]):
            out = io.StringIO()
            self.assertEqual(scope_check.main([*bad, str(Path(self.home, "x.md"))], out=out), 2)
            self.assertIn("check failed:", out.getvalue())
        out = io.StringIO()
        missing = str(Path(self.home, "missing.md"))
        self.assertEqual(scope_check.main(["--base", f"{REPO}={self.tree}", missing], out=out), 2)
        self.assertIn("check failed:", out.getvalue())

    def test_entrypoint(self):
        with (
            mock.patch.object(sys, "argv", [SCRIPT]),
            mock.patch("sys.stdout", io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            runpy.run_path(SCRIPT, run_name="__main__")
        self.assertEqual(raised.exception.code, 2)
