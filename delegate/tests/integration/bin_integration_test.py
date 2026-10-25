"""bin/ensemble-delegate run by absolute path from a temp cwd, no PYTHONPATH, a minimal PATH."""

import os
import re
import shutil
import subprocess
import sys
import tempfile

from delegate.tests.case import REPO, Case

BIN = os.path.join(REPO, "bin", "ensemble-delegate")
MINIMAL = "/usr/bin:/bin"
COMMANDS = [
    "run",
    "status",
    "content",
    "outcome",
    "resume",
    "show",
    "hide",
    "close",
    "attach-command",
    "prompt",
]


def system_python_ok() -> bool:
    try:
        done = subprocess.run(
            ("/usr/bin/python3", "-c", "import sys; sys.exit(sys.version_info < (3, 11))"),
            capture_output=True,
            timeout=30,
            check=False,
        )
    except OSError:
        return False
    return done.returncode == 0


class Command(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)

    def run_bin(self, path, *argv, bin_path=BIN):
        return subprocess.run(
            (bin_path, *argv),
            cwd=self.tmp,
            env={"HOME": self.home, "PATH": path},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def test_help_lists_the_subcommands_with_only_its_own_roots(self):
        """PATH is exactly /usr/bin:/bin where that python3 is 3.11+ (CI); elsewhere (macOS 3.9) a
        directory holding a python3 link to this interpreter comes first.
        """
        path = MINIMAL
        if not system_python_ok():
            own = os.path.join(self.tmp, "python")
            os.mkdir(own)
            os.symlink(sys.executable, os.path.join(own, "python3"))
            path = f"{own}:{MINIMAL}"
        done = self.run_bin(path, "--help")
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertEqual(re.search(r"\{([a-z,-]+)\}", done.stdout)[1].split(","), COMMANDS)

    def test_a_colon_in_its_path_reruns_it_through_a_link(self):
        """OpenCode caches the plugin under .../ensemble@git+https:/...: PATH and PYTHONPATH split on ":"."""
        repo = os.path.join(self.tmp, "ensemble@git+https:", "ensemble")
        os.makedirs(repo)
        for name in ("utils", "session", "delegate"):
            os.symlink(os.path.join(REPO, name), os.path.join(repo, name))
        shutil.copytree(os.path.join(REPO, "bin"), os.path.join(repo, "bin"))
        path = MINIMAL
        if not system_python_ok():
            own = os.path.join(self.tmp, "python")
            os.mkdir(own)
            os.symlink(sys.executable, os.path.join(own, "python3"))
            path = f"{own}:{MINIMAL}"
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
        repo = os.path.join(self.tmp, "repo")
        bin_dir = os.path.join(repo, "bin")
        os.makedirs(bin_dir)
        copy = os.path.join(bin_dir, "ensemble-delegate")
        shutil.copy2(BIN, copy)
        probe = os.path.join(bin_dir, "python3")
        with open(probe, "w", encoding="utf-8") as f:
            f.write('#!/bin/sh\nprintf "%s\\n" "$PATH" "$PYTHONPATH" "$*"\n')
        os.chmod(probe, 0o755)
        done = self.run_bin(MINIMAL, "status", "w", bin_path=copy)
        self.assertEqual(done.returncode, 0, done.stderr)
        roots = ":".join(os.path.join(repo, p, "src") for p in ("utils", "session", "delegate"))
        self.assertEqual(
            done.stdout.splitlines(),
            [f"{bin_dir}:{MINIMAL}", roots, "-P -m ensemble.delegate.cli status w"],
        )
