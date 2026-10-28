"""src/broker/internal/launcher/assets/workspace-check against real git repos."""

import runpy
import shutil
from pathlib import Path

from tests.case import REPO, Case
from tests.utils.sandbox import git

CHECK = Path(REPO, "src/broker/internal/launcher/assets/workspace-check")


class WorkspaceCheck(Case):
    def setUp(self):
        self.dirty = runpy.run_path(str(CHECK), run_name="workspace_check")["dirty"]
        self.root = Path(self.home, "work")
        self.remote = Path(self.home, "remote.git")
        git("init", "--bare", "-q", str(self.remote))

    def clone(self, rel):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        git("clone", "-q", str(self.remote), str(path))
        (path / "f").write_text(rel)
        git("add", "f", cwd=path)
        git("commit", "-qm", "c", cwd=path)
        git("push", "-q", "origin", "HEAD", cwd=path)
        return path

    def test_pushed_clones_and_cache_only_changes_are_clean(self):
        self.clone("src/o/a")
        self.assertEqual(self.dirty(str(self.root), ".ensemble-cache"), [])
        shutil.rmtree(self.root)
        repo = self.clone(".")
        (repo / ".ensemble-cache").mkdir()
        (repo / ".ensemble-cache" / "pkg").write_text("x")
        self.assertEqual(self.dirty(str(self.root), ".ensemble-cache"), [])

    def test_each_kind_of_unsaved_work_is_dirty(self):
        cases = {
            "untracked": lambda p: (p / "new").write_text("n"),
            "modified": lambda p: (p / "f").write_text("2"),
            "unpushed": lambda p: git("commit", "-qm", "e", "--allow-empty", cwd=p),
            "detached": lambda p: (
                git("checkout", "-q", "--detach", cwd=p),
                git("commit", "-qm", "d", "--allow-empty", cwd=p),
            ),
            "stashed": lambda p: ((p / "f").write_text("3"), git("stash", "-q", cwd=p)),
            "hidden-untracked": lambda p: (
                git("config", "status.showUntrackedFiles", "no", cwd=p),
                (p / "u").write_text("u"),
            ),
        }
        for name, change in cases.items():
            with self.subTest(name):
                repo = self.clone(f"src/o/{name}")
                change(repo)
                self.assertEqual(self.dirty(str(self.root), ".ensemble-cache"), [f"src/o/{name}"])
                shutil.rmtree(repo)

    def test_a_repo_under_a_dir_it_cannot_read_is_dirty(self):
        self.clone("src/o/a")
        locked = self.root / "src" / "o"
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o755)
        self.assertEqual(self.dirty(str(self.root), ".ensemble-cache"), ["src/o"])

    def test_an_empty_init_is_clean_and_a_broken_repo_is_dirty(self):
        git("init", "-q", str(self.root / "publish"))
        self.assertEqual(self.dirty(str(self.root), ".ensemble-cache"), [])
        broken = self.root / "src" / "o" / "b"
        broken.mkdir(parents=True)
        (broken / ".git").write_text("gitdir: /nowhere\n")
        self.assertEqual(self.dirty(str(self.root), ".ensemble-cache"), ["src/o/b"])
