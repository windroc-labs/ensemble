from pathlib import Path

from tests.case import REPO
from tests.guard_checks import commands, exports, ownership
from tests.guard_data.delegate import CONFIG_FILE, DELEGATE_SRC
from tests.guard_data.orchestrator import ORCH_FILES
from tests.guard_data.session import CLIENTS_HOME, SESSION_SRC
from tests.guard_samples import PKG, Sample

OPKG = "src/ensemble/orchestrator"


class DelegatePackageNames(Sample):
    def test_legacy_resource_access_is_rejected_in_production_only(self):
        base = "src/ensemble/delegate/"
        for sample in (
            "from ensemble.utils import read_project as read\nread('delegate')\n",
            "import ensemble.utils as u\nu.import_roots()\n",
            "DELEGATE = '/repo/delegate'\n",
            "from .config import IMPORT_ROOTS\n",
            "import legacy\ncommand = legacy.DELEGATE\n",
            "from ensemble.utils import LayerCommand\nLayerCommand.DELEGATE = 'x'\n",
        ):
            with self.subTest(sample=sample):
                root = self.tree({base + "engine/config.py": sample})
                self.assertTrue(ownership.delegate_package_names(root))
        root = self.tree(
            {
                base + "engine/config.py": "from ensemble.utils import read_resources\n",
                "tests/delegate/sample.py": "from ensemble.utils import read_project\n",
            }
        )
        self.assertEqual(ownership.delegate_package_names(root), [])

    def test_shared_layer_command_member_is_not_a_legacy_package_path(self):
        for sample in (
            "from ensemble.utils import LayerCommand\ncommand = LayerCommand.DELEGATE\n",
            "from ensemble.utils import LayerCommand as C\ncommand = C.DELEGATE\n",
            "import ensemble.utils as u\ncommand = u.LayerCommand.DELEGATE\n",
        ):
            with self.subTest(sample=sample):
                root = self.tree({"src/ensemble/delegate/cli.py": sample})
                self.assertEqual(ownership.delegate_package_names(root), [])


class SessionNames(Sample):
    def test_session_names_no_config_reader_protocol_tag_or_team_text(self):
        probe = SESSION_SRC + "engine/probe.py"
        for files, expected in (
            ({SESSION_SRC + "engine/config.py": "from ensemble.utils import read_resources\n"}, []),
            (
                {SESSION_SRC + "engine/config.py": "from ensemble.utils import read_project\n"},
                [SESSION_SRC + "engine/config.py: names read_project"],
            ),
            (
                {
                    probe: "import tomllib\nfrom ensemble.utils import read_project\nTAG = 'ensemble-progress'\nTEAM = 'team/'\n"
                },
                [
                    probe + ": names ensemble-progress",
                    probe + ": names read_project",
                    probe + ": names team/",
                    probe + ": names tomllib",
                ],
            ),
            (
                {
                    probe: "import json\n",
                    "src/ensemble/delegate/probe.py": "import tomllib\n",
                },
                [],
            ),
        ):
            with self.subTest(files=files):
                self.assertEqual(ownership.session_names(self.tree(files)), expected)


class OrchestratorText(Sample):
    def test_agent_label_examples_in_the_packaged_config_name_no_client(self):
        path = f"{OPKG}/config.toml"
        text = (
            "# local: [orchestrator.linear.agent_labels]\n"
            '# local: codex = "00000000-0000-4000-8000-000000000083"\n'
        )
        root = self.tree({path: text})
        self.assertEqual(ownership.orchestrator_text(root), [f"{path}:2: client name codex"])

    def test_local_client_limit_keys_in_the_packaged_config_may_name_a_client(self):
        path = f"{OPKG}/config.toml"
        text = (
            "# claude = 3\n"
            "[client_max_runs]\n"
            "opencode = 1  # one job\n"
            "# claude = 3\n"
            "codex = 2  # not claude\n"
            "# local: [orchestrator.client_max_runs]\n"
            "# local: opencode = 1\n"
            'claude = "x"\n'
            "[task_tiers]\n"
            "codex = 2\n"
        )
        root = self.tree({path: text})
        self.assertEqual(
            ownership.orchestrator_text(root),
            [
                f"{path}:10: client name codex",
                f"{path}:1: client name claude",
                f"{path}:3: client name opencode",
                f"{path}:4: client name claude",
                f"{path}:5: client name claude",
                f"{path}:5: client name codex",
                f"{path}:8: client name claude",
            ],
        )

    def test_client_names_tool_strings_and_handoff_literals_name_the_file_and_line(self):
        for files, expected in (
            (
                {
                    f"{OPKG}/router.py": 'X = "codex"\n',
                    f"{OPKG}/a.py": (
                        'import subprocess\nsubprocess.run(["tmux", "ls"])\nK = "clients"\n# Claude\n'
                    ),
                    f"{OPKG}/config.toml": '# claude\nx = "tmux"\ny = \'clients\'\nz = "tmux ls"  # "tmux"\n',
                },
                [
                    f"{OPKG}/config.toml:1: client name claude",
                    f'{OPKG}/config.toml:2: str constant "tmux"',
                    f'{OPKG}/config.toml:3: str constant "clients"',
                    f'{OPKG}/a.py:2: str constant "tmux"',
                    f'{OPKG}/a.py:3: str constant "clients"',
                    f"{OPKG}/a.py:4: client name Claude",
                    f"{OPKG}/router.py:1: client name codex",
                ],
            ),
            (
                {
                    f"{OPKG}/a.py": '"""Reads AGENTS.md."""\nP = "AGENTS.md"\nH = "tmux attach"\n',
                    f"{OPKG}/config.toml": '# see AGENTS.md\nx = "tmux attach"\n',
                },
                [],
            ),
            (
                {
                    f"{OPKG}/engine/flow/handoff.py": 'H = "Handoff from"\nS = ("## Source", "## Instructions", "## Comments")\n',
                    f"{OPKG}/promote.py": 'x = f"Handoff from {i}: {u}"\n# ## Comments are quoted\n',
                    f"{OPKG}/target.py": 'M = "or its ## Instructions"\nS = "## Source"\n',
                    f"{OPKG}/issues.py": 'H = "Handoff from"\n',
                    f"{OPKG}/config.toml": 'x = "## Comments"\n',
                    "tests/a_test.py": 'X = "Handoff from ## Source"\n',
                },
                [
                    f"{OPKG}/promote.py:1: Handoff literal Handoff from",
                    f"{OPKG}/promote.py:2: Handoff literal ## Comments",
                    f"{OPKG}/target.py:1: Handoff literal ## Instructions",
                    f"{OPKG}/target.py:2: Handoff literal ## Source",
                    f"{OPKG}/issues.py:1: Handoff literal Handoff from",
                ],
            ),
        ):
            with self.subTest(files=sorted(files)):
                self.assertEqual(ownership.orchestrator_text(self.tree(files)), sorted(expected))


class Standalone(Sample):
    def test_bytecode_caches_are_ignored_and_metadata_bytes_are_tolerated(self):
        root = Path(self.tree({"src/ensemble/session/engine/sample.py": ""}))
        cache = root / "src/ensemble/session/engine/__pycache__"
        cache.mkdir()
        (cache / "sample.cpython-311.pyc").write_bytes(b"\xa7\r\r\nensemble.orchestrator")
        (root / "src/ensemble/session/.DS_Store").write_bytes(b"\xff")
        self.assertEqual(ownership.standalone(str(root)), [])

    def test_any_file_of_a_lower_project_naming_the_orchestrator(self):
        team = f"{PKG}/delegate/team"
        root = self.tree(
            {
                "src/ensemble/delegate/config.toml": "# see ../orchestrator/\n",
                f"{team}/roles/r.md": (
                    "x\nRun ensemble.orchestrator.engine.run, not orchestrator/cli.py\n"
                ),
                f"{PKG}/session/x.py": "import ensemble.orchestrator\n",
                f"{PKG}/utils/y.py": "# [orchestrator] team\n",
                f"{PKG}/manager/z.py": "# ensemble/orchestrator/cli.py\n",
                f"{PKG}/__pycache__/x.pyc": "ensemble.orchestrator",
                "skills/a/notes": (
                    "the orchestrator's overlay, an `orchestrator` destination, sub_orchestrator/\n"
                ),
                "tests/session/a_test.py": "import ensemble.orchestrator\n",
                "orchestrator/b.py": "import ensemble.orchestrator\n",
                "tests/c.py": "import ensemble.orchestrator\n",
            }
        )
        self.assertEqual(
            ownership.standalone(root),
            [
                "src/ensemble/delegate/config.toml:1: names orchestrator/",
                f"{team}/roles/r.md:2: names ensemble.orchestrator",
                f"{team}/roles/r.md:2: names orchestrator/",
                f"{PKG}/manager/z.py:1: names ensemble/orchestrator",
                f"{PKG}/session/x.py:1: names ensemble.orchestrator",
                f"{PKG}/utils/y.py:1: names [orchestrator]",
            ],
        )


class ClientNames(Sample):
    def test_pinned_plugin_sources_are_data_only_in_session_plugin_config(self):
        config = "src/ensemble/session/config.toml"
        pinned = '"updraft@acme/claude-tools#' + "a" * 40 + '"'
        for path, text, expected in (
            (config, f"plugins = [{pinned}]\n", []),
            (config, f'plugins = [{pinned}, "codex"]\n', ["codex"]),
            (config, f"other = [{pinned}]\n", ["claude"]),
            (config, 'plugins = ["updraft@acme/claude-tools#main"]\n', ["claude"]),
            ("src/ensemble/delegate/config.toml", f"plugins = [{pinned}]\n", ["claude"]),
        ):
            with self.subTest(path=path, text=text):
                self.assertEqual(
                    ownership.client_names(self.tree({path: text})),
                    [f"{path}:1: client name {name}" for name in expected],
                )

    def test_client_names_find_opencode_outside_the_clients_package(self):
        outside = "src/ensemble/session/engine/x.py"
        inside = "src/ensemble/session/clients/opencode/common.py"
        text = 'CLIENT = "opencode"\n'
        self.assertEqual(
            ownership.client_names(self.tree({outside: text, inside: text})),
            [f"{outside}:1: client name opencode"],
        )

    def test_help_exemptions_preserve_same_line_code_and_interpolated_expressions(self):
        path = "src/ensemble/manager/cli.py"
        for text in (
            'HELP = "Examples: Codex"; CLIENT = "codex"\n',
            "HELP = f\"Examples: Codex {choose('codex')}\"\n",
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    ownership.client_names(self.tree({path: text})),
                    [f"{path}:1: client name codex"],
                )

    def test_display_help_exempts_returned_text_only(self):
        path = "src/ensemble/session/hosts/tmux/host.py"
        text = 'def display_help(client):\n    if client == "codex":\n        return "Codex placement"\n    return client_help("codex")\n'
        self.assertEqual(
            ownership.client_names(self.tree({path: text})),
            [f"{path}:2: client name codex", f"{path}:4: client name codex"],
        )

    def test_cli_help_examples_can_name_a_client_without_allowing_runtime_names(self):
        path = "src/ensemble/manager/cli.py"
        text = 'HELP = """Examples:\n  With Codex configured:\n  ensemble-session start helper --client codex\n"""\nCLIENT = "codex"\n'
        self.assertEqual(
            ownership.client_names(self.tree({path: text})), [f"{path}:5: client name codex"]
        )
        other = "src/ensemble/manager/engine/crew.py"
        self.assertTrue(ownership.client_names(self.tree({other: text})))

    def test_host_display_help_examples_do_not_exempt_runtime_client_names(self):
        path = "src/ensemble/session/hosts/tmux/host.py"
        text = 'def display_help():\n    return "Codex placement example"\nCLIENT = "codex"\n'
        self.assertEqual(
            ownership.client_names(self.tree({path: text})), [f"{path}:3: client name codex"]
        )

    def test_a_client_name_outside_clients_policy_tables_and_client_lines(self):
        config = "src/ensemble/delegate/config.toml"
        api = f"{PKG}/session/engine/api.py"
        for files, expected in (
            (
                {api: 'NAME = "Claude"\n# codex\n'},
                [f"{api}:1: client name Claude", f"{api}:2: client name codex"],
            ),
            ({config: "[policy.claude]\nflags = []\n[policy.codex]\nflags = []\n"}, []),
            ({f"{PKG}/session/clients/claude/x.py": 'NAME = "claude"\n'}, []),
            (
                {
                    config: '# codex in a comment\n[clients.codex]\nimplementation = "ensemble.session.clients.codex:CodexClient"  # codex\nkey = "x # codex"\n'
                },
                [f"{config}:4: client name codex"],
            ),
            (
                {config: 'implementation = "codex"\nimplementation = "my.codex:Codex"\n'},
                [f"{config}:1: client name codex"],
            ),
            (
                {
                    config: 'client = "codex"\n[roles.x]\nclient = "codex"\n[roles.y]\nclient = "codex"  # note\n'
                },
                [],
            ),
            (
                {
                    config: (
                        '[clients.codex]\nimplementation = "ensemble.session.clients.codex:CodexClient"\n[roles.pm]\nmodel = "codex"\nclient = ["codex"]\nclient = "codex\nclient = "codex" x\n'
                    )
                },
                [f"{config}:{n}: client name codex" for n in (4, 5, 6, 7)],
            ),
        ):
            with self.subTest(files=files):
                self.assertEqual(ownership.client_names(self.tree(files)), expected)


class ClientsKey(Sample):
    def test_the_literal_key_only_in_the_session_config_parser(self):
        sample = 'cfg = {}\nvalue = cfg["clients"]\n'
        root = self.tree(
            {
                f"{PKG}/delegate/composer/x.py": sample,
                f"{PKG}/session/clients/x.py": sample,
                CONFIG_FILE: sample,
                CLIENTS_HOME: sample,
            }
        )
        self.assertEqual(
            ownership.clients_key(root),
            [
                f'{PKG}/delegate/composer/x.py:2: literal key "clients"',
                f'{CONFIG_FILE}:2: literal key "clients"',
                f'{PKG}/session/clients/x.py:2: literal key "clients"',
            ],
        )


class TmuxCalls(Sample):
    def test_tmux_only_in_the_host_package_configuration_and_test_fixtures(self):
        sample = 'import subprocess\nsubprocess.run(["tmux", "kill-server"])\n'
        mode = f"{PKG}/session/modes/x.py"
        cli = f"{PKG}/session/cli.py"
        outside = "tmux outside session/hosts/tmux/, viewer/ and engine/tmux_cli.py"
        for files, expected in (
            (
                {
                    f"{PKG}/session/hosts/tmux/tmux_host.py": sample,
                    f"{PKG}/session/hosts/tmux/commands.py": sample,
                    f"{PKG}/session/hosts/tmux/model/validation.py": sample,
                    f"{PKG}/session/hosts/tmux_external/x.py": sample,
                    mode: sample,
                    f"{PKG}/delegate/composer/y.py": 'TOOL = "tmux"\n',
                    "src/ensemble/utils/hosts/tmux/tmux_host.py": sample,
                    "tests/session/z.py": sample,
                },
                sorted(
                    [
                        f'{PKG}/delegate/composer/y.py:1: str constant "tmux"',
                        'src/ensemble/utils/hosts/tmux/tmux_host.py:2: str constant "tmux"',
                        f"{mode}:2: {outside}",
                        f"{PKG}/session/hosts/tmux_external/x.py:2: {outside}",
                    ]
                ),
            ),
            (
                {mode: 'HINT = "tmux attach -t =s"\nPATH = "/usr/bin/tmux"\n'},
                [f"{mode}:{line}: {outside}" for line in (1, 2)],
            ),
            (
                {
                    cli: (
                        "from ensemble.session.hosts.tmux import TmuxHost\n"
                        "import ensemble.session.hosts.tmux as transport\n"
                        "def _tmux(tmux_path):\n"
                        "    return f'{transport.TmuxHost}'\n"
                        "class TmuxProcess: pass\n"
                    )
                },
                [f"{cli}:{line}: {outside}" for line in range(1, 6)],
            ),
            (
                {
                    f"{PKG}/session/config.toml": '[hosts]\ndefault = "tmux"\n',
                    "tests/session/unit/sample_test.py": 'HOST = "tmux"\n',
                },
                [],
            ),
        ):
            with self.subTest(files=sorted(files)):
                self.assertEqual(ownership.tmux_calls(self.tree(files)), expected)


class SessionDisplay(Sample):
    def test_display_names_are_rejected_in_each_scoped_source(self):
        for base in (
            f"{PKG}/session/cli.py",
            f"{PKG}/session/modes/sample.py",
            f"{PKG}/session/hosts/other/sample.py",
            f"{PKG}/utils/sample.py",
            CONFIG_FILE,
        ):
            for source in (
                "def f(pane): return pane\n",
                "def f(_pane): return _pane\n",
                "def f(pane_id): return pane_id\n",
                "def opener(): pass\n",
                "class split_from: pass\n",
                "x = value.opener\n",
                'x = f"{value.pane}"\n',
                "from transport import pane as screen\n",
                'x = "--split"\n',
                'x = "--split-from"\n',
                'x = "@opener"\n',
                'x = "@record"\n',
                'x = "@pane"\n',
            ):
                with self.subTest(base=base, source=source):
                    self.assertTrue(ownership.session_display(self.tree({base: source})))

    def test_the_host_implementation_other_packages_and_unrelated_words_are_clean(self):
        source = 'def f(pane, opener, split_from): return "@record"\n'
        for files in (
            {
                f"{PKG}/session/engine/tmux_cli.py": source,
                f"{PKG}/delegate/engine/other.py": source,
                f"{OPKG}/engine/sample.py": source,
                "tests/session/unit/sample_test.py": source,
            },
            {
                f"{PKG}/session/engine/sample.py": (
                    '@record\ndef f(display): return display.split("/")\n'
                    'screenplay = "panel"\nreopener = "reopener"\n'
                )
            },
        ):
            with self.subTest(files=sorted(files)):
                self.assertEqual(ownership.session_display(self.tree(files)), [])


class DelegateDisplay(Sample):
    def test_display_names_are_rejected_anywhere_in_delegate_source_only(self):
        named = 'def f(pane): return "@opener"\n'
        for files, expected in (
            *(
                ({base: source}, [f"{base}:1: interprets display {name!r}"])
                for base in (DELEGATE_SRC + "cli.py", DELEGATE_SRC + "engine/sample.py")
                for source, name in (
                    ("def f(pane): return pane\n", "pane"),
                    ("def f(pane_id): return pane_id\n", "pane_id"),
                    ("def opener(): pass\n", "opener"),
                    ("class split_from: pass\n", "split_from"),
                    ("x = value.opener\n", "opener"),
                    ('x = "--split"\n', "--split"),
                    ('x = "--split-from"\n', "--split-from"),
                    ('x = "@opener"\n', "@opener"),
                    ('x = "@record"\n', "@record"),
                    ('x = "@pane"\n', "@pane"),
                )
            ),
            (
                {
                    DELEGATE_SRC + "engine/sample.py": (
                        '@record\ndef f(display): return display.split("/")\nscreenplay = "reopener"\n'
                    ),
                    "tests/delegate/unit/sample_test.py": named,
                    f"{PKG}/session/hosts/tmux/sample.py": named,
                },
                [],
            ),
        ):
            with self.subTest(files=files):
                self.assertEqual(ownership.delegate_display(self.tree(files)), expected)


class OrchestratorDisplay(Sample):
    def test_display_names_are_rejected_anywhere_in_orchestrator_source_only(self):
        named = 'def f(pane): return "@opener"\n'
        for files, expected in (
            *(
                ({base: source}, [f"{base}:1: interprets display {name!r}"])
                for base in (ORCH_FILES + "cli.py", ORCH_FILES + "engine/sample.py")
                for source, name in (
                    ("def f(pane): return pane\n", "pane"),
                    ("def opener(): pass\n", "opener"),
                    ("class split_from: pass\n", "split_from"),
                    ("x = value.opener\n", "opener"),
                    ('x = "--split"\n', "--split"),
                    ('x = "@opener"\n', "@opener"),
                    ('x = "@record"\n', "@record"),
                    ('x = "@pane"\n', "@pane"),
                )
            ),
            (
                {
                    ORCH_FILES + "engine/sample.py": (
                        '@record\ndef f(display): return display.split("/")  # a pane\n'
                    ),
                    "tests/orchestrator/sample_test.py": named,
                    f"{PKG}/session/hosts/tmux/sample.py": named,
                },
                [],
            ),
        ):
            with self.subTest(files=files):
                self.assertEqual(ownership.orchestrator_display(self.tree(files)), expected)

    def test_the_real_tree_is_clean(self):
        self.assertEqual(ownership.orchestrator_display(REPO), [])


class DelegateTmux(Sample):
    def test_tmux_in_any_case_anywhere_in_delegate_python_source_is_rejected(self):
        base = DELEGATE_SRC + "engine/sample.py"
        for files, expected in (
            *(
                ({base: source}, [f"{base}:1: names tmux"])
                for source in (
                    "from ensemble.session import TmuxHost\n",
                    "def _tmux(): pass\n",
                    'HOST = "tmux"\n',
                    'LINE = f"run {name} in TMUX"\n',
                    "x = 1  # a Tmux pane\n",
                    '"""Runs under tmux."""\n',
                )
            ),
            (
                {
                    base: "MULTIPLEXER = 'tm-ux'\n",
                    DELEGATE_SRC + "team/guide.md": "tmux\n",
                    "tests/delegate/unit/sample_test.py": 'HOST = "tmux"\n',
                    f"{PKG}/session/cli.py": "# tmux\n",
                },
                [],
            ),
        ):
            with self.subTest(files=files):
                self.assertEqual(ownership.delegate_tmux(self.tree(files)), expected)


class StatusLogFormat(Sample):
    CLOCK = 'CLOCK = "%H:%M:%S"\n'

    def test_the_real_tree_is_clean(self):
        self.assertEqual(ownership.status_log_format(REPO), [])

    def test_only_status_log_owns_the_clock(self):
        outside = "status log line format outside session/engine/status_log.py"
        for files, expected in (
            (
                {
                    f"{PKG}/delegate/x.py": self.CLOCK,
                    f"{PKG}/manager/y.py": 'import time\n\nline = f"{time.strftime(\'%H:%M:%S\')} {name}"\nhook = f"$(date +%H:%M:%S) {name} dead"\n',
                    f"{OPKG}/z.py": 'import time\nat = time.strftime("%H:%M:%S")\n',
                },
                sorted(
                    [
                        f"{PKG}/delegate/x.py:1: {outside}",
                        f"{PKG}/manager/y.py:3: {outside}",
                        f"{PKG}/manager/y.py:4: {outside}",
                        f"{OPKG}/z.py:2: {outside}",
                    ]
                ),
            ),
            (
                {
                    f"{PKG}/session/engine/status_log.py": self.CLOCK,
                    f"{PKG}/session/hosts/tmux/tmux_host.py": self.CLOCK,
                    f"{PKG}/session/hosts/tmux/lifecycle.py": self.CLOCK,
                    f"{PKG}/session/hosts/tmux/model/clock.py": self.CLOCK,
                    "tests/session/x_test.py": self.CLOCK,
                    "tests/y_test.py": self.CLOCK,
                },
                [
                    f"{PKG}/session/hosts/tmux/lifecycle.py:1: {outside}",
                    f"{PKG}/session/hosts/tmux/model/clock.py:1: {outside}",
                    f"{PKG}/session/hosts/tmux/tmux_host.py:1: {outside}",
                ],
            ),
            (
                {f"{PKG}/session/hosts/tmux_external/x.py": self.CLOCK},
                [f"{PKG}/session/hosts/tmux_external/x.py:1: {outside}"],
            ),
        ):
            with self.subTest(files=sorted(files)):
                self.assertEqual(ownership.status_log_format(self.tree(files)), expected)


class ManagerBoundaries(Sample):
    def test_short_launch_prefixes_are_allowed(self):
        base = "src/ensemble/manager/engine/crew.py"
        root = self.tree(
            {
                base: 'a = ("ensemble-session", "start", key, *argv)\n'
                'b = ("ensemble-orchestrator", "router", "--issue", key, "--tui", *argv)\n'
                'c = ("ensemble-delegate", "run", "--role", key, "--detach", "--name", name, *argv)\n'
            }
        )
        self.assertEqual(commands.manager_commands(root), [])

    def test_only_orphan_listing_is_hardcoded(self):
        base = "src/ensemble/manager/engine/crew.py"
        root = self.tree(
            {
                base: 'LS = ("ensemble-session", "ls", "--here", "--records")\nBAD = ["ensemble-session", "status"]\n'
            }
        )
        self.assertEqual(
            commands.manager_commands(root), [f"{base}:2: hardcoded layer command ensemble-session"]
        )

    def test_cli_and_other_manager_modules_are_scanned(self):
        base = "src/ensemble/manager/"
        root = self.tree(
            {
                base + "cli.py": 'a = "ensemble-session"\nb = "ensemble-crew"\n',
                base + "engine/other.py": 'a = ("ensemble-delegate", "status")\n',
            }
        )
        self.assertEqual(len(commands.manager_commands(root)), 2)

    def test_listing_accepts_only_the_two_record_forms(self):
        base = "src/ensemble/manager/engine/crew.py"
        for suffix, count in (("", 0), (', "--descendants"', 0), (', "--all"', 1)):
            root = self.tree(
                {base: f'a = ("ensemble-session", "ls", "--here", "--records"{suffix})\n'}
            )
            self.assertEqual(len(commands.manager_commands(root)), count)

    def test_status_vocabulary_uses_enum_members(self):
        base = "src/ensemble/manager/cli.py"
        for source, count in (
            ('suffix = "-drive"', 1),
            ('word = "needs_input"', 1),
            ('if status.outcome == "done": pass', 1),
            ('if "working" == status.state: pass', 1),
            ('if status.outcome in ("done", "failed"): pass', 1),
            ("if status.state != StatusState.GONE: pass", 0),
            ("if entry.state == State.GONE: pass", 0),
            ('class State:\n    DONE = "done"', 0),
        ):
            with self.subTest(source=source):
                root = self.tree({base: source})
                self.assertEqual(len(commands.manager_status_words(root)), count)

    def test_display_is_opaque(self):
        base = "src/ensemble/manager/engine/crew.py"
        root = self.tree({base: 'def f(pane): return "@opener"\n'})
        self.assertEqual(len(ownership.manager_display(root)), 2)
        clean = self.tree({base: 'def f(display): return display.split(":")\n'})
        self.assertEqual(ownership.manager_display(clean), [])

    def test_exports_require_a_literal_all_of_names_and_reject_functions_and_modules(self):
        base = "src/ensemble/manager/"
        init = base + "__init__.py"
        for files, expected in (
            (
                {
                    init: 'from ensemble.manager.engine.crew import f\n__all__ = ["f"]\n',
                    base + "engine/crew.py": "def f(): pass\n",
                },
                [f"{init}:2: exports function f"],
            ),
            ({init: ""}, [f"{init}: missing __all__"]),
            (
                {
                    init: 'class Crew: pass\n__all__ = ["Crew"]\n',
                    "src/ensemble/utils/__init__.py": 'def f(): pass\n__all__ = ["f"]\n',
                },
                [],
            ),
            (
                {
                    init: 'from os import getcwd as CWD\nimport os as OS\ndef f(): pass\ng = f\nLAMBDA = lambda: 0\n__all__ = ["CWD", "OS", "g", "LAMBDA"]\n'
                },
                [
                    f"{init}:6: exports function CWD",
                    f"{init}:6: exports function LAMBDA",
                    f"{init}:6: exports function g",
                    f"{init}:6: exports nonconstant OS",
                ],
            ),
            ({init: 'from pathlib import Path\nVALUE = "one"\n__all__ = ["Path", "VALUE"]\n'}, []),
            *(
                ({init: source}, [f"{init}:1: __all__ must be a literal sequence of names"])
                for source in ("__all__ = discover()\n", "__all__ = [1]\n")
            ),
        ):
            with self.subTest(files=files):
                self.assertEqual(exports.manager_exports(self.tree(files)), expected)

    def test_crew_never_reads_an_inherited_sid(self):
        base = "src/ensemble/manager/engine/crew.py"
        for source in (
            'import os\nx = os.getenv("ENSEMBLE_SID")\n',
            'import os\nx = os.environ.get("ENSEMBLE_SID")\n',
            'import os\nx = os.environ["ENSEMBLE_SID"]\n',
        ):
            self.assertEqual(
                ownership.manager_sid(self.tree({base: source})),
                [f"{base}:2: reads inherited ENSEMBLE_SID"],
            )
        self.assertEqual(
            ownership.manager_sid(self.tree({base: 'env = {"ENSEMBLE_SID": sid}\n'})), []
        )

    def test_roster_has_no_session_liveness_callback(self):
        base = "src/ensemble/manager/engine/roster.py"
        self.assertEqual(
            ownership.manager_lease(
                self.tree(
                    {base: 'class Roster:\n    def probe(self): return self._alive("manager")\n'}
                )
            ),
            [f"{base}:2: lease liveness callback"],
        )
