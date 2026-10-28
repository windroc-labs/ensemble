"""bin/ensemble-delegate run by absolute path from a temp cwd, no PYTHONPATH, a minimal PATH."""

import os
import re
import shutil

from tests.delegate.case import REPO, Case
from tests.delegate.cli_case import COMMANDS
from tests.utils.bin_fixtures import MINIMAL, minimal, probe_copy, run
from tests.utils.sandbox import scratch

BIN = os.path.join(REPO, "bin", "ensemble-delegate")


class Command(Case):
    def setUp(self):
        self.tmp = scratch(self)

    def run_bin(self, path, *argv, bin_path=BIN):
        return run(bin_path, path, *argv, cwd=self.tmp, home=self.home)

    def test_help_lists_the_subcommands_with_only_its_own_roots(self):
        done = self.run_bin(minimal(self.tmp), "--help")
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertEqual(re.search(r"\{([a-z,-]+)\}", done.stdout)[1].split(","), COMMANDS)

    def test_a_colon_in_its_path_reruns_it_through_a_link(self):
        """OpenCode caches the plugin under .../ensemble@git+https:/...: PATH and PYTHONPATH split on ":"."""
        repo = os.path.join(self.tmp, "ensemble@git+https:", "ensemble")
        os.makedirs(repo)
        os.symlink(os.path.join(REPO, "src"), os.path.join(repo, "src"))
        shutil.copytree(os.path.join(REPO, "bin"), os.path.join(repo, "bin"))
        path = minimal(self.tmp)
        for _ in range(2):
            done = self.run_bin(
                path, "--help", bin_path=os.path.join(repo, "bin", "ensemble-delegate")
            )
            self.assertEqual((done.returncode, done.stderr), (0, ""))
            self.assertEqual(re.search(r"\{([a-z,-]+)\}", done.stdout)[1].split(","), COMMANDS)
        links = os.path.join(self.home, ".ensemble", "links")
        [link] = os.listdir(links)
        self.assertEqual(os.readlink(os.path.join(links, link)), repo)

    def test_it_sets_pythonpath_to_three_roots_and_puts_its_dir_first_on_path(self):
        copy = probe_copy(self.tmp, BIN)
        bin_dir = os.path.dirname(copy)
        done = self.run_bin(MINIMAL, "status", "w", bin_path=copy)
        self.assertEqual(done.returncode, 0, done.stderr)
        roots = os.path.join(self.tmp, "repo", "src")
        self.assertEqual(
            done.stdout.splitlines(),
            [f"{bin_dir}:{MINIMAL}", roots, "-P -m ensemble.delegate.cli status w"],
        )
