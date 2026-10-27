"""modes/base.py: BaseMode, the ABC each Mode's running session implements (a class start
returning it started, then its session calls), and the start helpers the modes share: child_env,
identity_drop and _select_account.
"""

import inspect
import itertools
import os
import tempfile
from unittest import mock

from ensemble.session.clients.base import Client
from ensemble.session.clients.codex import CodexClient
from ensemble.session.clients.registry import Registry
from ensemble.session.engine.types import Access, ConfigError, Launch, Liveness, Mode, Spec
from ensemble.session.hosts.base import Host
from ensemble.session.modes import base, headless, in_process, tui
from ensemble.session.modes.base import BaseMode, child_env, identity_drop
from ensemble.session.modes.client_adapters.headless import Headless
from ensemble.utils import ACCOUNT_ENV as ACCOUNT
from ensemble.utils import load_class
from tests.session.case import HOSTS, Case
from tests.session.fakes.terminal import MemoryHost

MEMBERS = ["poll", "returncode", "screen", "send", "start", "state", "stop"]
KINDS = {
    headless.HeadlessMode: Mode.HEADLESS,
    in_process.InProcessMode: Mode.IN_PROCESS,
    tui.TuiMode: Mode.TUI,
}
LEASE = {
    "ENSEMBLE_MANAGER",
    "ENSEMBLE_HOLDER_ID",
    "ENSEMBLE_HOLDER_GENERATION",
    "ENSEMBLE_HOLDER_PID",
}
GUARD = {"ENSEMBLE_GUARD_LEASE_FD", "ENSEMBLE_GUARD_HOOK_SOCKET"}
IDENTITIES = {
    "ENSEMBLE_SID",
    "ENSEMBLE_EVENTS",
    *LEASE,
    *GUARD,
    "INNER_SESSION",
    "INNER_ID",
    "OTHER_SESSION",
    "OTHER_ID",
}


class Launches(Headless):
    """A headless mode recording each launch it is asked for."""

    def __init__(self):
        self.asked = []

    def launch(self, spec):
        self.asked.append(spec)
        return Launch(("x",))

    def events(self, lines):
        return iter(())


class HeadlessClient(Client):
    def __init__(self, config):
        super().__init__(config)
        self.mode = Launches()

    def headless(self):
        return self.mode


class NoModeClient(Client):
    def headless(self):
        return None

    def tui(self):
        return None


class InnerClient(NoModeClient):
    """A client whose sessions set INNER_SESSION in their children."""

    session_env = "INNER_SESSION"
    identity_env = ("INNER_SESSION", "INNER_ID")
    child_defaults = {"PAGER": "cat", "NO_COLOR": "1"}


class OtherClient(NoModeClient):
    session_env = "OTHER_SESSION"
    identity_env = ("OTHER_SESSION", "OTHER_ID")
    child_defaults = {"OTHER_MODE": "on"}


class AccountsClient(Client):
    """A client with registered accounts default and work, and every mode (none ever run)."""

    def headless(self):
        return mock.Mock()

    def tui(self):
        return mock.Mock()

    def in_process(self):
        return mock.Mock()

    def _accounts(self):
        return frozenset({"default", "work"})


def registry(**clients) -> Registry:
    """A Registry of `clients` (client → Client class, None: one failing to load)."""

    def load(path, _base):
        found = classes[path]
        if found is None:
            raise ValueError(f"{path}: cannot import")
        return found

    classes = {f"tests.{name}:{name.capitalize()}Client": cls for name, cls in clients.items()}
    return Registry(
        clients={
            name: {"implementation": f"tests.{name}:{name.capitalize()}Client"} for name in clients
        },
        hosts=Host.parse_hosts({**HOSTS, "default": "tmux"}),
        class_loader=load,
    )


class BaseModeTest(Case):
    def test_its_abstract_members_are_one_start_and_the_six_session_calls(self):
        self.assertTrue(inspect.isabstract(BaseMode))
        self.assertEqual(sorted(BaseMode.__abstractmethods__), MEMBERS)

    def test_each_mode_subclasses_it_with_its_parameters_and_its_kind(self):
        for mode, name in itertools.product(KINDS, MEMBERS):
            with self.subTest(mode=mode.__name__, name=name):
                self.assertTrue(issubclass(mode, BaseMode))
                self.assertFalse(inspect.isabstract(mode))
                self.assertEqual(params(getattr(mode, name)), params(getattr(BaseMode, name)))
        self.assertEqual({mode: mode.kind for mode in KINDS}, KINDS)

    def test_close_stops_the_run_unless_a_mode_overrides_it(self):
        run = mock.Mock()
        BaseMode.close(run)
        self.assertEqual(run.mock_calls, [mock.call.stop()])

    def test_only_a_tui_mode_may_have_a_launch_input_pending(self):
        for cls in (headless.HeadlessMode, in_process.InProcessMode):
            with self.subTest(cls=cls.__name__):
                self.assertIsNone(object.__new__(cls).opening_pending)

    def test_client_alive_is_its_state_live_unless_a_mode_overrides_it(self):
        run = mock.Mock()
        for state, alive in ((Liveness.LIVE, True), (Liveness.UNKNOWN, False)):
            run.state.return_value = state
            self.assertIs(BaseMode.client_alive(run), alive)

    def test_only_a_tui_mode_may_report_its_client_up(self):
        for cls in (headless.HeadlessMode, in_process.InProcessMode):
            with self.subTest(cls=cls.__name__):
                self.assertFalse(object.__new__(cls).client_up)

    def test_start_is_the_class_entry_and_no_session_is_built_without_its_prepared(self):
        for mode in (BaseMode, *KINDS):
            with self.subTest(mode=mode.__name__):
                self.assertIsInstance(inspect.getattr_static(mode, "start"), classmethod)
                self.assertIsInstance(inspect.getattr_static(mode, "prepared"), property)
                self.assertIsNone(inspect.getattr_static(mode, "prepared").fset)
        for mode in KINDS:
            with self.subTest(mode=mode.__name__), self.assertRaises(TypeError):
                mode()

    def test_a_failed_start_returns_no_session_and_records_nothing(self):
        spec = Spec(workdir="/w", access=Access(cwd="/w"), client="none", sid="s1")
        for mode, kind in KINDS.items():
            recorded = []
            with self.subTest(mode=kind.value), self.assertRaises(ConfigError) as caught:
                mode.start(
                    spec,
                    NoModeClient({}),
                    registry=registry(),
                    record=recorded.append,
                    host=MemoryHost(),
                )
            self.assertEqual(
                str(caught.exception), f"client 'none' does not support {kind.value} mode"
            )
            self.assertEqual(recorded, [])

    def test_a_start_inside_an_agent_session_is_refused_before_its_launch(self):
        client, recorded = HeadlessClient({}), []
        spec = Spec(workdir="/w", access=Access(cwd="/w"), client="headless", sid="s1")
        with (
            mock.patch.dict(os.environ, {"ENSEMBLE_AGENT_SESSION": "1"}),
            self.assertRaises(ConfigError) as caught,
        ):
            headless.HeadlessMode.start(
                spec, client, registry=registry(), record=recorded.append, host=MemoryHost()
            )
        self.assertTrue(str(caught.exception).startswith("refused inside an agent session"))
        self.assertEqual((client.mode.asked, recorded), ([], []))


class AccountTest(Case):
    def spec(self, client="claude", **env) -> Spec:
        return Spec(workdir="/w", access=Access(cwd="/w"), client=client, sid="s1", env=env)

    def test_no_or_an_empty_selection_is_no_account_on_any_client(self):
        for client, env in itertools.product(
            (AccountsClient({}), NoModeClient({})), ({}, {ACCOUNT: ""})
        ):
            with self.subTest(client=type(client).__name__, env=env):
                self.assertEqual(base._select_account(self.spec(**env), client), "")

    def test_a_registered_name_is_the_selected_account(self):
        for name in ("default", "work"):
            with self.subTest(name=name):
                spec = self.spec(**{ACCOUNT: name})
                self.assertEqual(base._select_account(spec, AccountsClient({})), name)

    def test_an_unregistered_name_is_a_config_error_naming_the_typed_clients_table(self):
        for client, name in (("claude", "nope"), ("claude", "Bad!"), ("claude-work", "nope")):
            with self.subTest(client=client, name=name), self.assertRaises(ConfigError) as caught:
                base._select_account(self.spec(client, **{ACCOUNT: name}), AccountsClient({}))
            self.assertEqual(
                str(caught.exception),
                f"account {name!r}: not a registered account of client {client}"
                f" ([clients.{client}.accounts.{name}])",
            )

    def test_a_client_without_accounts_refuses_any_selection(self):
        for client in ("codex", "opencode"):
            with self.subTest(client=client), self.assertRaises(ConfigError) as caught:
                base._select_account(self.spec(client, **{ACCOUNT: "work"}), NoModeClient({}))
            self.assertEqual(
                str(caught.exception), f"client {client} has no accounts: remove account 'work'"
            )

    def test_the_account_variable_never_reaches_a_child_inherited_or_given(self):
        self.enterContext(mock.patch.dict(os.environ, {ACCOUNT: "outer", "KEEP": "k"}))
        for inherit in (True, False):
            with self.subTest(inherit=inherit):
                launch = Launch(("x",), env={ACCOUNT: "given", "SET": "1"})
                env = child_env(launch, "/cwd", inherit=inherit)
                self.assertNotIn(ACCOUNT, env)
                self.assertEqual((env["SET"], env.get("KEEP", "k")), ("1", "k"))

    def test_every_mode_refuses_a_bad_selection_before_any_host_call(self):
        for mode, call, env in itertools.product(
            KINDS, ("start", "preflight"), ({ACCOUNT: "nope"}, {ACCOUNT: "Bad!"})
        ):
            host, recorded = mock.Mock(), []
            more = {"record": recorded.append} if call == "start" else {}
            with (
                self.subTest(mode=mode.kind.value, call=call, env=env),
                self.assertRaises(ConfigError) as caught,
            ):
                getattr(mode, call)(
                    self.spec(**env), AccountsClient({}), registry=registry(), host=host, **more
                )
            self.assertTrue(str(caught.exception).startswith(f"account {env[ACCOUNT]!r}: "))
            self.assertEqual((host.mock_calls, recorded), ([], []))


class ChildEnvTest(Case):
    def environ(self, **values):
        self.enterContext(
            mock.patch.dict(os.environ, {"HOME": os.environ["HOME"], **values}, clear=True)
        )

    def test_the_environment_less_drop_and_unset_then_the_launch_env_and_pwd(self):
        self.environ(KEEP="k", DROPPED="d", UNSET="u", SET="old")
        launch = Launch(("x",), env={"SET": "new", "ADDED": "1"}, unset=("UNSET",))
        self.assertEqual(
            child_env(launch, "/cwd", {"DROPPED"}),
            {
                "HOME": os.environ["HOME"],
                "KEEP": "k",
                "SET": "new",
                "ADDED": "1",
                "PWD": "/cwd",
                "ENSEMBLE_AGENT_SESSION": "1",
            },
        )
        self.assertEqual(child_env(launch, "/c")["PWD"], "/c")

    def test_launch_env_keys_need_no_allowlist_and_may_bring_back_a_dropped_one(self):
        self.environ(KEEP="k", OTHER_MODE="on")
        launch = Launch(("x",), env={"EDITOR": "vi", "ZED": "1", "OTHER_MODE": "on"})
        env = child_env(launch, "/cwd", {"OTHER_MODE"})
        self.assertEqual(
            {k: env[k] for k in ("KEEP", "EDITOR", "ZED", "OTHER_MODE")},
            {"KEEP": "k", "EDITOR": "vi", "ZED": "1", "OTHER_MODE": "on"},
        )

    def test_every_client_carries_the_agent_session_marker_whatever_it_inherits_or_is_given(self):
        self.assertEqual(child_env(Launch(("x",)), "/cwd")["ENSEMBLE_AGENT_SESSION"], "1")
        self.environ(ENSEMBLE_AGENT_SESSION="0")
        for launch in (
            Launch(("x",)),
            Launch(("x",), env={"ENSEMBLE_AGENT_SESSION": ""}),
            Launch(("x",), unset=("ENSEMBLE_AGENT_SESSION",)),
        ):
            with self.subTest(launch=launch):
                env = child_env(launch, "/cwd", {"ENSEMBLE_AGENT_SESSION"})
                self.assertEqual(env["ENSEMBLE_AGENT_SESSION"], "1")


class PluginPathTest(ChildEnvTest):
    """FR-32: a session's PATH starts with the running plugin's bin/, the directory where PATH
    finds ensemble-session (each plugin bin/ script puts its own first).
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.plugin = os.path.join(os.path.realpath(tmp.name), "plugin root", "bin")
        os.makedirs(self.plugin)
        command = os.path.join(self.plugin, "ensemble-session")
        with open(command, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\n")
        os.chmod(command, 0o755)

    def test_the_plugin_bin_goes_first_only_when_it_finds_ensemble_session_and_leads_once(self):
        inherited = os.pathsep.join(("/usr/bin", self.plugin, "/bin"))
        led = os.pathsep.join((self.plugin, "/usr/bin"))
        for path, given, expected in (
            (inherited, None, os.pathsep.join((self.plugin, "/usr/bin", self.plugin, "/bin"))),
            (
                inherited,
                "/opt/old/bin:/usr/bin",
                os.pathsep.join((self.plugin, "/opt/old/bin", "/usr/bin")),
            ),
            (led, None, led),
            ("/usr/bin:/bin", None, "/usr/bin:/bin"),
            ("/usr/bin:/bin", "/opt/bin", "/opt/bin"),
        ):
            with self.subTest(path=path, given=given):
                self.environ(PATH=path)
                launch = Launch(("x",)) if given is None else Launch(("x",), env={"PATH": given})
                self.assertEqual(child_env(launch, "/cwd")["PATH"], expected)


class IdentityDropTest(ChildEnvTest):
    def test_every_configured_clients_identity_env(self):
        self.environ(INNER_ID="i", OTHER_SESSION="", OTHER_ID="o", KEEP="k")
        self.assertEqual(identity_drop(registry(inner=InnerClient, other=OtherClient)), IDENTITIES)
        self.assertEqual(
            identity_drop(registry()),
            frozenset({"ENSEMBLE_SID", "ENSEMBLE_EVENTS", *LEASE, *GUARD}),
        )

    def test_identity_drop_drops_guard_variables(self):
        """A start from inside a managed worker never hands its guard's lease or socket on."""
        self.environ(ENSEMBLE_GUARD_LEASE_FD="7", ENSEMBLE_GUARD_HOOK_SOCKET="/run/hook")
        env = child_env(Launch(("x",)), "/cwd", identity_drop(registry()))
        self.assertFalse(GUARD & set(env))

    def test_a_callers_start_token_roster_lease_and_events_file_never_reach_a_client(self):
        for environ in (
            {"ENSEMBLE_SID": "0f8fad5b-d9cb-469f-a165-70867728950e"},
            dict.fromkeys(LEASE, "1"),
            {"ENSEMBLE_EVENTS": "/harness/events"},
        ):
            with self.subTest(environ=environ):
                self.environ(**environ, KEEP="k")
                env = child_env(Launch(("x",)), "/cwd", identity_drop(registry()))
                self.assertEqual((environ.keys() & env.keys(), env["KEEP"]), (set(), "k"))
        given = Launch(("x",), env={"ENSEMBLE_EVENTS": "/given/events"})
        env = child_env(given, "/cwd", identity_drop(registry()))
        self.assertEqual(env["ENSEMBLE_EVENTS"], "/given/events")

    def test_a_child_default_only_inside_its_client_and_at_its_value(self):
        both = registry(inner=InnerClient, other=OtherClient)
        for environ, defaults in (
            ({"PAGER": "cat", "NO_COLOR": "1", "OTHER_MODE": "on"}, set()),
            (
                {"INNER_SESSION": "1", "PAGER": "cat", "NO_COLOR": "0", "OTHER_MODE": "on"},
                {"PAGER"},
            ),
            (
                {"OTHER_SESSION": "1", "PAGER": "cat", "NO_COLOR": "1", "OTHER_MODE": "on"},
                {"OTHER_MODE"},
            ),
        ):
            with self.subTest(environ=environ):
                self.environ(**environ)
                self.assertEqual(identity_drop(both) - IDENTITIES, defaults)

    def test_codex_injected_defaults_only_inside_codex_at_their_injected_value(self):
        inside = {"CODEX_THREAD_ID": "t"}
        for environ, dropped in (
            ({"CLAUDECODE": "1", "NO_COLOR": "1", "PAGER": "cat"}, set()),
            ({**inside, "NO_COLOR": "0", "PAGER": "less", "CODEX_CI": "x"}, set(inside)),
            (
                {**inside, "NO_COLOR": "1", "PAGER": "cat", "GIT_PAGER": "cat", "CODEX_CI": "1"},
                {*inside, "NO_COLOR", "PAGER", "GIT_PAGER", "CODEX_CI"},
            ),
        ):
            with self.subTest(environ=environ):
                self.environ(**environ)
                self.assertEqual(identity_drop(registry(codex=CodexClient)) & set(environ), dropped)

    def test_it_reads_class_data_and_builds_no_client(self):
        class Unbuilt(InnerClient):
            def __init__(self, config):
                raise AssertionError("built")

        self.environ(INNER_SESSION="1", PAGER="cat", OTHER_ID="o")
        self.assertEqual(
            identity_drop(registry(inner=Unbuilt, other=OtherClient)), {*IDENTITIES, "PAGER"}
        )

    def test_a_client_failing_to_load_is_a_config_error(self):
        with self.assertRaises(ConfigError) as caught:
            identity_drop(registry(inner=InnerClient, broken=None))
        self.assertEqual(
            str(caught.exception),
            "[clients.broken].implementation: tests.broken:BrokenClient: cannot import",
        )


def params(function):
    return [
        (None if p.kind is inspect.Parameter.POSITIONAL_ONLY else p.name, p.kind, p.default)
        for p in inspect.signature(function).parameters.values()
    ]


class OwnerTest(Case):
    """A tui, headless or in_process start can be owned by a session harness."""

    def test_tui_headless_and_in_process_modes_have_a_generation(self):
        self.assertIsNone(BaseMode.generation.fget(mock.Mock(spec=BaseMode)))
        for mode in (tui.TuiMode, headless.HeadlessMode, in_process.InProcessMode):
            self.assertIsNot(mode.generation, BaseMode.generation)

    def test_only_a_tmux_host_is_harnessed(self):
        self.assertFalse(Host._harnessed)
        self.assertFalse(MemoryHost._harnessed)
        tmux = Host.parse_hosts({**HOSTS, "default": "tmux"}).tables["tmux"]["implementation"]
        self.assertTrue(load_class(tmux, Host)._harnessed)
