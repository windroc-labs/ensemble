"""Work-directory overlap checks through real filesystem symlinks."""

import os

from tests.orchestrator.case import REPO, Case
from tests.orchestrator.config_fixtures import BASE, PIPELINE, ConfigFile


class WorkDirectories(ConfigFile, Case):
    def test_refused(self):
        d = os.path.realpath(self.dir)
        clink = os.path.join(d, "clink")
        os.makedirs(os.path.join(d, "c"))
        os.symlink(os.path.join(d, "c"), clink)

        def clone(path):
            return f'[local_clones]\n"acme/x" = "{path}"\n'

        def in_clone(path):
            return f"work_dir must neither lie in nor contain local_clones.acme/x {path}: "

        cases = [
            (f'work_dir = "{d}/c"\n', clone(f"{d}/c"), f"{in_clone(d + '/c')}{d + '/c'!r}"),
            (f'work_dir = "{clink}"\n', clone(f"{d}/c/x"), f"{in_clone(d + '/c/x')}{clink!r}"),
            (f'work_dir = "{clink}/w"\n', clone(f"{d}/c"), f"{in_clone(d + '/c')}{clink + '/w'!r}"),
            ('work_dir = "w"\n', "", "work_dir must be a printable absolute or ~ path: 'w'"),
            ("work_dir = 5\n", "", "work_dir must be a printable absolute or ~ path: 5"),
        ]
        for text, tail, msg in cases:
            with self.subTest(text=text, tail=tail):
                self.fails(f"orchestrator/config.toml: {msg}", text + BASE + tail)

    def test_the_repo_checkout_is_a_valid_work_dir(self):
        real = os.path.realpath(REPO)
        link = os.path.join(os.path.realpath(self.dir), "link")
        os.symlink(real, link)
        for work, want in (
            (real, real),
            (f"{real}/w", f"{real}/w"),
            (f"{link}/w", f"{real}/w"),
            (os.path.dirname(real), os.path.dirname(real)),
        ):
            with self.subTest(work=work):
                self.assertEqual(self.load(f'work_dir = "{work}"\n' + PIPELINE).work_dir, want)
