"""Real Git and interpreter contracts in the test sandbox."""

import os
import shutil
import tempfile

from ensemble.orchestrator.engine import target
from ensemble.utils import SHORT, checkout
from orchestrator_tests.case import Case
from orchestrator_tests.target_fixtures import LOCAL
from orchestrator_tests.unit.target_fixtures import FakeRun, ok

from delegate.tests.fakes.delegate import FakeDelegate
from delegate.tests.workdir import checkout_path
from utils.tests.sandbox import git


class TargetCheckout(Case):
    T = target.Target("acme", "ensemble", "TASK-26-x")

    HTTPS = "https://github.com/acme/ensemble.git"

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.work = os.path.join(self.tmp, "work")
        self.workdir = os.path.join(self.work, "TASK-26")
        self.checkout = checkout_path(self.workdir, "acme", "ensemble", "TASK-26")
        self.src = os.path.dirname(os.path.dirname(self.checkout))

    def clone(self, name, origin=HTTPS):
        path = os.path.join(self.tmp, name)
        git("init", "-q", path)
        git("-C", path, "remote", "add", "origin", origin)
        return path

    def fresh(self):
        shutil.rmtree(self.checkout, ignore_errors=True)
        os.makedirs(self.checkout)
        return os.path.join(self.checkout, ".git")

    def git_file(self, text):
        with open(self.fresh(), "w") as f:
            f.write(text)

    def worktree(self, clone):
        admin = os.path.join(self.tmp, "admin")
        os.makedirs(admin, exist_ok=True)
        with open(os.path.join(admin, "commondir"), "w") as f:
            f.write(os.path.join(clone, ".git") + "\n")
        self.git_file(f"gitdir: {admin}\n")
        return admin

    def run_(self, clones, *, writable=(), run=None):
        return target.with_clone(
            self.T,
            "TASK-26",
            clones=clones,
            delegate=FakeDelegate,
            workdir=self.workdir,
            writable=writable,
            run=run,
        )

    def want(self, clone):
        return target.Target("acme", "ensemble", "TASK-26-x", clone)

    def test_the_checkout_is_the_one_its_delegate_reports_by_cores_rule(self):
        def found():
            return target.checkout(
                "TASK-26", "acme", "ensemble", delegate=FakeDelegate, workdir=self.workdir
            )

        self.assertIsNone(found())
        self.fresh()
        self.assertEqual(found(), self.checkout)
        self.assertEqual(self.checkout, checkout(self.src, "acme", "ensemble", "TASK-26"))

    def test_a_worktree_at_the_old_owner_name_path_is_not_read(self):
        x, y = self.clone("x"), self.clone("y")
        self.worktree(x)
        os.rename(self.checkout, os.path.join(os.path.dirname(self.checkout), "ensemble"))
        self.assertEqual(self.run_({"acme/ensemble": y}), self.want(y))

    def test_without_a_checkout_the_table_entry_found_case_insensitively(self):
        y = self.clone("y")
        for clones, want in (
            ({"acme/other": "/o", "acme/ensemble": y}, y),
            ({"acme/other": "/o", "Acme/Ensemble": y}, y),
            ({}, ""),
            ({"acme/other": "/o", "acme/ensemble-2": "/p"}, ""),
        ):
            with self.subTest(clones):
                self.assertEqual(self.run_(clones), self.want(want))
        os.makedirs(os.path.join(self.src, "acme"))
        self.assertEqual(self.run_({}), self.want(""))

    def test_a_clone_made_from_owner_name_beats_the_table(self):
        os.makedirs(self.fresh())
        self.assertEqual(self.run_({"acme/ensemble": self.clone("y")}), self.want(""))

    def test_a_worktree_of_the_targets_clone_beats_the_table(self):
        y = self.clone("y")
        for name, origin in (("x", self.HTTPS), ("x-scp", "git@github.com:Acme/Ensemble.git")):
            x = self.clone(name, origin)
            self.worktree(x)
            for clones in ({"acme/ensemble": y}, {}):
                with self.subTest(origin=origin, clones=clones):
                    self.assertEqual(self.run_(clones), self.want(x))

    def test_the_only_git_run_is_the_guarded_origin_read_of_the_clone(self):
        x = self.clone("x")
        self.worktree(x)
        fake = FakeRun([(LOCAL, ok(self.HTTPS + "\n"))])
        self.assertEqual(self.run_({}, run=fake), self.want(x))
        self.assertEqual(fake.calls, [((*LOCAL, x, "remote", "get-url", "origin"), SHORT)])
        for setup in (lambda: os.makedirs(self.fresh()), lambda: shutil.rmtree(self.checkout)):
            setup()
            fake.calls.clear()
            self.run_({"acme/ensemble": x}, run=fake)
            self.assertEqual(fake.calls, [])

    def test_a_clone_under_writable_is_not_used(self):
        x, y = self.clone("x"), self.clone("y")
        self.worktree(x)
        self.assertEqual(self.run_({"acme/ensemble": y}, writable=(self.tmp,)), self.want(y))
        self.assertEqual(
            self.run_({"acme/ensemble": y}, writable=(os.path.join(self.tmp, "elsewhere"),)),
            self.want(x),
        )
        self.assertEqual(self.run_({}, writable=(self.tmp,)), self.want(""))

    def test_the_default_writable_holds_the_workdirs(self):
        inside = os.path.join(self.work, "clone")
        git("init", "-q", inside)
        git("-C", inside, "remote", "add", "origin", self.HTTPS)
        y = self.clone("y")
        self.worktree(inside)
        self.assertEqual(self.run_({"acme/ensemble": y}, writable=None), self.want(y))

    def test_the_table_decides_when_the_worktrees_clone_is_not_usable(self):
        y = self.clone("y")
        other = self.clone("other", "https://github.com/acme/other.git")
        gone = self.clone("gone")
        self.worktree(other)
        self.assertEqual(self.run_({"acme/ensemble": y}), self.want(y))
        self.worktree(gone)
        shutil.rmtree(gone)
        self.assertEqual(self.run_({"acme/ensemble": y}), self.want(y))
        newline = self.clone("x\ny")
        self.worktree(newline)
        self.assertEqual(self.run_({"acme/ensemble": y}), self.want(y))
        self.git_file("nonsense\n")
        self.assertEqual(self.run_({"acme/ensemble": y}), self.want(y))
        self.git_file("gitdir: " + os.path.join(self.tmp, "no-such-gitdir") + "\n")
        self.assertEqual(self.run_({"acme/ensemble": y}), self.want(y))
