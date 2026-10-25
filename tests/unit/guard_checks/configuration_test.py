import ast
import os

from tests.case import ORCH_ROOT, REPO
from tests.guard_checks import configuration, ownership
from tests.guard_data.orchestrator import CONFIG_PY, LOAD_HELPERS
from tests.guard_data.session import SESSION_SRC
from tests.guard_samples import Sample

OPKG = "src/ensemble/orchestrator"


class ConfigOnce(Sample):
    LOAD = "calls ensemble.orchestrator.engine.config.load outside an entry main"
    USE_LOGS = "calls ensemble.orchestrator.engine.log.use_logs outside an entry main"

    def test_load_use_logs_and_load_helpers_outside_an_entry_main_or_config_py(self):
        for files, expected in (
            (
                {
                    f"{OPKG}/engine/prune.py": "from ensemble.orchestrator.engine import config\nfrom ensemble.orchestrator.engine import log as linear\ndef main():\n    config.load()\n    linear.use_logs(None)\n\ndef inner():\n    config.load()\n",
                    f"{OPKG}/engine/writeback.py": (
                        "from ensemble.orchestrator.engine import config\nfrom ensemble.orchestrator.engine.log import use_logs\n\ndef main():\n    config.load()\n    use_logs(None)\n"
                    ),
                    f"{OPKG}/engine/promote.py": (
                        "import ensemble.orchestrator.engine.config as c\n\nclass P:\n    def main(self):\n        c.load()\n"
                    ),
                    f"{OPKG}/run.py": "import ensemble.orchestrator.engine.config\n\ndef main():\n    ensemble.orchestrator.engine.config.load()\n",
                },
                [
                    f"{OPKG}/engine/promote.py:5: {self.LOAD}",
                    f"{OPKG}/engine/prune.py:8: {self.LOAD}",
                    f"{OPKG}/engine/writeback.py:5: {self.LOAD}",
                    f"{OPKG}/engine/writeback.py:6: {self.USE_LOGS}",
                    f"{OPKG}/run.py:4: {self.LOAD}",
                ],
            ),
            (
                {
                    f"{OPKG}/engine/run.py": (
                        "from ensemble.orchestrator.engine import config\nconfig._runnable(1)\n"
                    ),
                    f"{OPKG}/engine/config.py": (
                        "def _runnable():\n    pass\n\ndef load():\n    _runnable()\n    _docs()\n    _load_config()\n"
                    ),
                    f"{OPKG}/engine/promote.py": (
                        "from ensemble.orchestrator.engine import config\nfrom ensemble.orchestrator.engine import log as linear\ndef main():\n    try:\n        cfg = config.load()\n    except SystemExit:\n        linear.use_logs(None)\n"
                    ),
                    f"{OPKG}/config.py": "def load():\n    _runnable()\n",
                },
                [
                    f"{OPKG}/config.py:2: calls _runnable",
                    f"{OPKG}/engine/run.py:2: calls ensemble.orchestrator.engine.config._runnable",
                ],
            ),
        ):
            with self.subTest(files=sorted(files)):
                self.assertEqual(configuration.config_once(self.tree(files)), expected)

    def test_the_load_helpers_are_config_py_functions(self):
        with open(os.path.join(REPO, CONFIG_PY), encoding="utf-8") as f:
            defined = {n.name for n in ast.parse(f.read()).body if isinstance(n, ast.FunctionDef)}
        self.assertLessEqual(set(LOAD_HELPERS), defined)

    def test_the_real_entries_calls_outside_an_entry(self):
        files = {}
        for name in ("promote.py", "prune.py"):
            with open(
                os.path.join(ORCH_ROOT, "ensemble", "orchestrator", "engine", name),
                encoding="utf-8",
            ) as f:
                files[f"{OPKG}/not_{name}"] = f.read()
        found = configuration.config_once(self.tree(files))
        self.assertEqual(
            {(f.split(":")[0], f.split(": ", 1)[1]) for f in found},
            {(rel, call) for rel in files for call in (self.LOAD, self.USE_LOGS)},
        )


class OrchestratorAbsent(Sample):
    def test_an_absent_or_empty_tree_is_clean(self):
        for root in (os.path.join(self.tree({}), "missing"), self.tree({"config.toml": ""})):
            for guard in (ownership.orchestrator_text, configuration.config_once):
                with self.subTest(guard.__name__, root=root):
                    self.assertEqual(guard(root), [])


class ExplicitKeys(Sample):
    def test_each_local_example_key_needs_a_real_line_in_the_packaged_defaults(self):
        orch = f"{OPKG}/config.toml"
        for name, files, expected in (
            (
                "packaged defaults supply the keys",
                {
                    SESSION_SRC + "config.toml": "[hosts.tmux]\nstatus_line = false\n",
                    "src/ensemble/delegate/config.toml": (
                        "# local: [session.hosts.tmux]\n# local: status_line = true\n"
                    ),
                },
                [],
            ),
            (
                "a commented-only key",
                {
                    SESSION_SRC
                    + "config.toml": "[hosts.tmux]\nx = 1\n# local: [hosts.tmux]\n# local: status_line = true\n"
                },
                [
                    f"{SESSION_SRC}config.toml: `# local:` key hosts.tmux.status_line has no real line"
                ],
            ),
            (
                "a real line, its shape suffix or an empty table",
                {
                    "src/ensemble/delegate/config.toml": (
                        'cwd = ""\n# local: cwd = "/x"\n[output]\ntype = "local"\ndir = ""\n[roles.a.output]\ntype = "x"\n# local: [roles.b]\n# local: cwd = "/y"\n# local: [roles.b.output]\n# local: dir = "d"\n'
                    ),
                    orch: (
                        '[labels]\n# local: [labels]\n# local: x = "1"\n[roles.a]\nkey = ""\n# local: [roles.c]\n# local: key = "k"\n# local: [core.roles.a]\n# local: cwd = "/z"\n'
                    ),
                },
                [],
            ),
            (
                "sectioned examples",
                {
                    orch: (
                        '[labels]\n# local: [orchestrator.labels]\n# local: x = "1"\n'
                        '[roles.a]\nkey = ""\n# local: [orchestrator.roles.b]\n# local: key = "k"\n'
                        "# local: [orchestrator]\n# local: typo = 1\n# local: [session]\n"
                    )
                },
                [f"{orch}: `# local:` key orchestrator.typo has no real line"],
            ),
            (
                "a project root config.toml",
                {
                    SESSION_SRC + "config.toml": "[hosts.tmux]\nstatus_line = false\n",
                    "session/config.toml": "# local: [hosts.tmux]\n# local: typo = 1\n[bad\n",
                },
                [],
            ),
        ):
            with self.subTest(name):
                self.assertEqual(configuration.explicit_keys(self.tree(files)), expected)

    def test_a_commented_only_key_is_refused(self):
        root = self.tree(
            {
                SESSION_SRC + "config.toml": (
                    "[hosts.tmux]\nx = 1\n# local: [hosts.tmux]\n# local: status_line = true\n"
                ),
                "src/ensemble/delegate/config.toml": (
                    '# local: team_dirs = ["~/t"]\n[roles.a]\nt = 1\n# local: [roles.a]\n# local: [roles.a.deep]\n# local: y = 2\n'
                ),
                f"{OPKG}/config.toml": "# local: [bad\n",
            }
        )
        bad = f"{OPKG}/config.toml: "
        found = configuration.explicit_keys(root)
        self.assertEqual(
            [f for f in found if not f.startswith(bad)],
            [
                "src/ensemble/delegate/config.toml: `# local:` key roles.a.deep.y has no real line",
                "src/ensemble/delegate/config.toml: `# local:` key team_dirs has no real line",
                SESSION_SRC + "config.toml: `# local:` key hosts.tmux.status_line has no real line",
            ],
        )
        self.assertEqual(len([f for f in found if f.startswith(bad)]), 1)
