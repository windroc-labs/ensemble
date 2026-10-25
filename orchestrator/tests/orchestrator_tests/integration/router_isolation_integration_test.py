"""Real Git and interpreter contracts in the test sandbox."""

import json
import os

from ensemble.utils import GUARD, SHORT
from orchestrator_tests.router_fixtures import (
    DOCS_SEARCH,
    ENG_RUN,
    INPUT,
    LS_REMOTE,
    OUTER,
    SEARCHED,
    OuterBase,
    pr_search,
)
from orchestrator_tests.unit.run_fixtures import (
    ID,
)
from orchestrator_tests.unit.target_fixtures import ok

from delegate.tests.workdir import checkout_path
from utils.tests.sandbox import git


class RunLaunching(OuterBase):
    def local_clone(self):
        path = os.path.realpath(os.path.join(self.tmp, "clone"))
        for argv in (
            ["init", "-q", path],
            ["-C", path, "remote", "add", "origin", "https://github.com/acme/ensemble.git"],
        ):
            git(*argv)
        self.write_config(OUTER + f'[local_clones]\n"Acme/Ensemble" = "{path}"\n')
        return path

    def test_a_configured_local_clone_is_the_inputs_repo(self):
        clone = self.local_clone()
        self.assertEqual(self.outer(), 0)
        self.assertEqual(self.input(), INPUT.replace("Repo: Acme/Ensemble", f"Repo: {clone}"))
        self.assertEqual(
            self.run_.calls,
            [
                (("gh", "api", "repos/acme/ensemble"), SHORT),
                (LS_REMOTE, SHORT),
                *DOCS_SEARCH,
                pr_search("session", "registry", repo="Acme/Ensemble"),
            ],
        )
        self.assertEqual(self.err, "")

    def test_research_and_design_name_the_local_clone(self):
        clone = self.local_clone()
        for role in ("researcher", "pm"):
            with self.subTest(role=role):
                self.run_.table = SEARCHED
                self.assertEqual(self.outer(role=role), 0)
                self.assertEqual(self.detached(role).input.splitlines()[1], f"Repo: {clone}")
                self.assertIsNone(json.loads(self.raw_runs().splitlines()[-1])["target"])

    def test_a_resume_over_a_clone_checkout_keeps_its_repo(self):
        self.local_clone()
        cfg = self.cfg()
        found = checkout_path(cfg.issue_workdir(ID), "Acme", "Ensemble", ID)
        os.makedirs(os.path.join(found, ".git"))
        self.api.checkouts[cfg.issue_workdir(ID)] = (found,)
        self.run_.table = [(("git", *GUARD, "-C"), ok()), *ENG_RUN]
        self.assertEqual(self.outer(mode="resume"), 0)
        self.assertEqual(self.input(), INPUT)
        self.assertEqual([argv[0] for argv, _ in self.run_.calls], ["gh", "git", "git"])
