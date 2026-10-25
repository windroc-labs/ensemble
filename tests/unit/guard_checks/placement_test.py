import os

from tests import guards
from tests.guard_checks import imports, placement
from tests.guard_data.orchestrator import ORCH_PKG, ORCH_PROJECT
from tests.guard_data.session import SESSION_SRC
from tests.guard_samples import PKG, Sample

OPKG = "src/ensemble/orchestrator"


UNLISTED = (
    "not __init__.py, cli.py, config.toml, engine/, a listed subpackage or a listed entry module"
)


class PackageClimbs(Sample):
    def violations(self, text, sub="engine/sample.py"):
        rel = f"{PKG}/delegate/{sub}"
        root = self.tree({rel: text})
        return [v for v in placement.package_climbs(root) if v.startswith(f"{rel}:")]

    def test_rejects_every_way_to_climb_or_escape_the_package(self):
        for text in (
            *(
                f"import os\nfrom pathlib import Path\nROOT = {expr}\n"
                for expr in (
                    "os.path.join(os.path.dirname(__file__), *[os.pardir] * 2)",
                    "os.path.dirname(os.path.dirname(os.path.dirname(__file__)))",
                    "Path(__file__).parents[2]",
                    "Path(__file__).parents[-1]",
                    "Path(__file__).parents[index]",
                    "Path(__file__).parents[1:3]",
                    "Path(__file__).parent.parent.parent",
                    "Path(__file__).resolve().parent / '..' / '..'",
                )
            ),
            "from pathlib import Path as P\nf = P(__file__)\np = f.parent.parent\nx = p.parent\n",
            "import os as o\np = o.path.dirname(__file__)\nx = o.path.join(p, '..', '..')\n",
            "from pathlib import Path\np = Path(__file__).parent.parent.parent / 'session' / 'config.toml'\n",
            "import os\nx = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'orchestrator/config.toml')\n",
            "import os\nfrom ensemble.utils.paths import ROOT\nx = os.listdir(ROOT)\n",
            "from ensemble.utils import ROOT\n",
            "from ensemble.utils.paths import import_roots\n",
            "from ensemble.utils import ROOT\nfrom os.path import join\nx = join(ROOT, 'bin')\n",
            "from ensemble.utils.paths import ROOT as root\nfrom pathlib import Path\nx = Path(root) / 'bin'\n",
            "from ensemble import utils\nx = utils.ROOT\n",
            "import ensemble.utils.paths as paths\nx = paths.import_roots\n",
        ):
            with self.subTest(text=text):
                self.assertTrue(self.violations(text))

    def test_paths_inside_the_package_unrelated_bases_and_repository_tools_pass(self):
        sample = "engine/sample.py"
        for text, sub in (
            (
                "from pathlib import Path\np = Path(__file__).parent / 'session' / 'config.toml'\n",
                sample,
            ),
            (
                "from pathlib import Path\np = Path(__file__).parent.parent / 'session' / 'config.toml'\n",
                sample,
            ),
            (
                "import os\nx = os.path.join(os.path.dirname(__file__), 'orchestrator/config.toml')\n",
                sample,
            ),
            (
                "import os\nx = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'orchestrator/config.toml')\n",
                sample,
            ),
            (
                "import os\nfrom pathlib import Path\nx = os.path.join('/data', 'session')\ny = Path('/work') / 'delegate'\n",
                sample,
            ),
            (
                "import os\nTEAM = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'team')\n",
                "composer/base.py",
            ),
            (
                "import os\nHOOK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'hosts/tmux/tmux_host.py')\n",
                "modes/tui_hook.py",
            ),
        ):
            with self.subTest(text=text):
                self.assertEqual(self.violations(text, sub), [])
        root = self.tree(
            {"tests/delegate/test.py": "from pathlib import Path\nx = Path(__file__).parents[9]\n"}
        )
        self.assertFalse(any("test.py" in v for v in placement.package_climbs(root)))

    def test_no_file_is_exempt(self):
        rel = "src/ensemble/utils/paths.py"
        root = self.tree(
            {
                rel: "import os\nROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), *[os.pardir] * 4))\n"
            }
        )
        self.assertEqual(
            placement.package_climbs(root), [f"{rel}:2: ROOT: path escapes its package"]
        )
        self.assertEqual(placement.package_climbs(self.tree({})), [])


class NamespacePackage(Sample):
    def own(self, *extra: str) -> str:
        files = [f"src/ensemble/{sub}/__init__.py" for sub in guards.PROJECTS.values()]
        files.append("src/broker/go.mod")
        return self.tree({f: "" for f in (*files, *extra)})

    def test_shared_namespace_and_broker_allow_only_the_source_packages(self):
        for extra, expected in (
            (("src/__pycache__/x.pyc", f"{PKG}/__pycache__/x.pyc"), []),
            (("src/.DS_Store", f"{PKG}/.DS_Store", "src/ensemble.egg-info/PKG-INFO"), []),
            ((f"{PKG}/__init__.py",), [f"{PKG}/__init__.py: namespace package __init__.py"]),
            (
                (f"{PKG}/config.py", "src/repo.py", "src/other/__init__.py"),
                [
                    f"{PKG}/config.py: not a source package",
                    "src/other: not a source package",
                    "src/repo.py: not a source package",
                ],
            ),
        ):
            with self.subTest(extra=extra):
                self.assertEqual(placement.namespace_package(self.own(*extra)), expected)

    def test_a_missing_subpackage(self):
        root = self.own()
        os.remove(os.path.join(root, PKG, "manager", "__init__.py"))
        os.rmdir(os.path.join(root, PKG, "manager"))
        self.assertEqual(placement.namespace_package(root), [f"{PKG}/manager: missing"])


class OrchestratorLayout(Sample):
    def test_only_cli_is_executable_no_run_module_and_the_entry_is_required(self):
        for files, expected in (
            (
                {
                    f"{OPKG}/cli.py": 'if __name__ == "__main__": pass\n',
                    f"{OPKG}/engine/router.py": 'if __name__ == "__main__": pass\n',
                },
                [f"{OPKG}/engine/router.py:1: entry outside cli.py"],
            ),
            (
                {
                    f"{OPKG}/cli.py": "",
                    f"{OPKG}/engine/run.py": "from ensemble import delegate\n",
                },
                [f"{OPKG}/engine/run.py: no such module: a run is its Delegate's harness (FR-25)"],
            ),
            ({}, [f"{OPKG}/cli.py: missing entry"]),
        ):
            with self.subTest(files=sorted(files)):
                self.assertEqual(placement.orchestrator_entry(self.tree(files)), expected)

    def test_session_imports_are_frozen_at_their_owners(self):
        root = self.tree(
            {
                f"{OPKG}/engine/router.py": "from ensemble.session import SessionError, RunLog\n",
                f"{OPKG}/engine/ops.py": "from ensemble.session import SessionError\n",
                f"{OPKG}/engine/new.py": "import ensemble.session as session\nsession.RunLog()\n",
            }
        )
        self.assertEqual(
            imports.orchestrator_session_imports(root),
            [
                f"{OPKG}/engine/new.py: {ORCH_PKG}.engine.new imports RunLog from ensemble.session; go through ensemble.delegate",
                f"{OPKG}/engine/ops.py: {ORCH_PKG}.engine.ops imports SessionError from ensemble.session; go through ensemble.delegate",
                f"{OPKG}/engine/router.py: {ORCH_PKG}.engine.router imports RunLog from ensemble.session; go through ensemble.delegate",
                f"{OPKG}/engine/router.py: {ORCH_PKG}.engine.router imports SessionError from ensemble.session; go through ensemble.delegate",
            ],
        )


class PackageTop(Sample):
    def package(self, project, *entries):
        sub = guards.PROJECTS[project]
        return {f"{PKG}/{sub}/{e}": "" for e in entries}

    def test_only_init_cli_engine_listed_subpackages_entry_modules_and_config(self):
        resource = {
            path: text
            for project in guards.PROJECTS
            for path, text in self.package(project, "__init__.py", "engine/x.py").items()
        }
        resource[SESSION_SRC + "config.toml"] = ""
        unlisted = {
            **self.package("utils", "__init__.py", "x.py"),
            **self.package(
                "session", "__init__.py", "extension.py", "engine/x.py", "modes/x.py", "extra.py"
            ),
            **self.package(
                "delegate",
                "__init__.py",
                "cli.py",
                "config.py",
                "engine/x.py",
                "team/x.py",
                "misc/x.py",
            ),
            **self.package(ORCH_PROJECT, "__init__.py", "engine/x.py", "config.toml"),
            **self.package("manager", "__init__.py", "engine/crew.py", "engine/roster.py", "y.py"),
        }
        for name, files, expected in (
            ("config resource", resource, []),
            (
                "unlisted",
                unlisted,
                [
                    f"{PKG}/delegate/misc: {UNLISTED}",
                    f"{PKG}/manager/y.py: {UNLISTED}",
                    f"{PKG}/session/extra.py: {UNLISTED}",
                ],
            ),
        ):
            with self.subTest(name):
                self.assertEqual(placement.package_top(self.tree(files)), expected)

    def test_a_missing_entry(self):
        files = self.package("delegate", "__init__.py", "cli.py")
        self.assertIn(f"{PKG}/delegate/engine: missing", placement.package_top(self.tree(files)))
