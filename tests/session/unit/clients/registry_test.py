"""clients/registry.py: what a caller-owned Registry stores (read-only copies of the
[clients.<name>] tables and the parsed Hosts, never parsed again), settings() from a read session
config ([viewer] included), the tui settings a configured class's default Host reads from [hosts.tmux] and Client loading
from module.path:Class. Caller-owned fake module tables supply the client loader; each test
constructs fresh settings.
"""

import abc
import importlib
import importlib.abc
import importlib.util
import json
import operator
import os
import sys
import tomllib
import types
import unittest
from typing import Any
from unittest import mock

import ensemble.session.hosts.tmux
from ensemble.session import Access, Host, Spec
from ensemble.session.clients import registry
from ensemble.session.clients.base import Client
from ensemble.session.clients.claude import ClaudeClient
from ensemble.session.engine.config import read_config
from ensemble.session.engine.hosted_session import HostedSession
from ensemble.session.engine.types import ConfigError, Hosts, TuiSettings
from ensemble.utils import load_class
from tests.session import fake_client, harnesses
from tests.session.case import HOSTS, SID, Case, configured
from tests.session.modules import client_loader
from tests.utils.sandbox import scratch

FAKE_CLASS = "ensemble.session.clients.fake:FakeClient"
FAKE = f'[clients.fake]\nimplementation = "{FAKE_CLASS}"\n'
MISSING = (
    "[clients.{n}].implementation: ensemble.session.clients.{n}:{c}Client: cannot import "
    "ensemble.session.clients.{n}: No module named 'ensemble.session.clients.{n}'"
)


class OddClient:
    pass


class HeadlessOnlyClient(Client):
    def headless(self):
        return fake_client.FakeHeadless(self)


class AbstractClient(Client, abc.ABC):
    @abc.abstractmethod
    def extra(self): ...


def odd_client():
    return None


def tui(cls: type[HostedSession]) -> TuiSettings:
    return cls.host().tui_settings()


def registry_of(cls: type[HostedSession]) -> registry.Registry:
    return cls._config().registry


def package(name, **names) -> types.ModuleType:
    """An in-memory module ensemble.session.clients.<name> holding `names`."""
    module = types.ModuleType(f"ensemble.session.clients.{name}")
    vars(module).update(names)
    return module


class SourceFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Finds module `name` and runs `source` as its body, as if it were a file on sys.path."""

    def __init__(self, name, source):
        self.name, self.source = name, source

    def find_spec(self, fullname, path, target=None):
        return importlib.util.spec_from_loader(fullname, self) if fullname == self.name else None

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        exec(self.source, vars(module))


class Base(Case):
    def configure(self, text, tmux=None):
        """Configure a session class from TOML clients, HOSTS and supplied tmux settings."""
        self.session_cls = configured(
            tomllib.loads(text).get("clients", {}),
            tmux,
            class_loader=client_loader(getattr(self, "modules", {})),
        )

    def register(self, **modules):
        self.modules = {
            **getattr(self, "modules", {}),
            **{f"ensemble.session.clients.{name}": module for name, module in modules.items()},
        }
        if hasattr(self, "session_cls"):
            registry_of(self.session_cls).class_loader = client_loader(self.modules)

    def refused(self, message, call):
        with self.assertRaises(ConfigError) as caught:
            call()
        self.assertEqual(str(caught.exception), message)


class ConfigureTest(Base):
    def test_max_runs_is_a_session_setting_not_an_adapter_setting(self):
        self.register(fake=fake_client)
        for limit in (0, 1, 3):
            with self.subTest(limit=limit):
                self.configure(FAKE + f"max_runs = {limit}\n")
                found = registry_of(self.session_cls)
                self.assertEqual(found.max_runs("fake"), limit)
                self.assertEqual(found.client("fake").config, {})
        self.configure(FAKE)
        self.assertEqual(registry_of(self.session_cls).max_runs("fake"), 0)

    def test_invalid_max_runs_is_rejected_at_configuration(self):
        for value in (True, False, -1, "2", 1.5, [], {}):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(
                    ConfigError, r"\[clients.fake\].max_runs: want a whole number >= 0"
                ),
            ):
                configured({"fake": {"implementation": FAKE_CLASS, "max_runs": value}})

    def test_configured_the_clients_and_tui_settings_are_read_without_a_file(self):
        self.register(fake=fake_client)
        with mock.patch("builtins.open") as opened:
            self.configure(FAKE + 'scenario = "/s"\n[clients.a]\nk = 1\n', {"status_line": True})
            self.assertEqual(registry_of(self.session_cls).configured(), ["a", "fake"])
            self.assertEqual(
                registry_of(self.session_cls).client("fake").config, {"scenario": "/s"}
            )
            self.assertEqual(
                tui(self.session_cls),
                TuiSettings(status_line=True),
            )
        opened.assert_not_called()

    def test_independent_classes_keep_their_own_configuration(self):
        self.register(fake=fake_client)
        self.configure(FAKE + 'scenario = "/a"\n', {"status_line": True})
        first = self.session_cls
        self.configure(FAKE + 'scenario = "/b"\n')
        self.assertEqual(registry_of(first).client("fake").config, {"scenario": "/a"})
        self.assertTrue(tui(first).status_line)
        self.assertEqual(registry_of(self.session_cls).client("fake").config, {"scenario": "/b"})
        self.assertEqual(tui(self.session_cls), TuiSettings())

    def test_the_client_tables_are_deep_copies(self):
        self.register(fake=fake_client)
        given = {
            "fake": {
                "implementation": FAKE_CLASS,
                "log": {"a": [1]},
            }
        }
        self.session_cls = configured(
            given, class_loader=client_loader(getattr(self, "modules", {}))
        )
        given["fake"]["log"]["a"].append(2)
        given["fake"]["scenario"] = "/s"
        given["other"] = {}
        self.assertEqual(registry_of(self.session_cls).client("fake").config, {"log": {"a": [1]}})
        self.assertEqual(registry_of(self.session_cls).configured(), ["fake"])

    def test_the_settings_are_read_only_and_each_client_gets_its_own_copy(self):
        self.register(fake=fake_client)
        self.configure(FAKE + "[clients.fake.log]\na = [1]\n", {"status_line": True})
        found = registry_of(self.session_cls).settings
        self.assertEqual(found.clients["fake"]["log"]["a"], (1,))
        for table, key in (
            (found.clients, "other"),
            (found.clients["fake"], "scenario"),
            (found.clients["fake"]["log"], "b"),
            (found.hosts, "ghost"),
            (found.hosts["tmux"], "status_line"),
        ):
            with self.subTest(key=key), self.assertRaises(TypeError):
                operator.setitem(table, key, {})
        first, second = (registry_of(self.session_cls).client("fake") for _ in range(2))
        self.assertIsNot(first, second)
        first.config["log"]["a"].append(2)
        self.assertEqual(second.config, {"log": {"a": [1]}})
        self.assertEqual(found.clients["fake"]["log"]["a"], (1,))

    def test_a_client_gets_lists_and_tuples_as_given_also_through_settings_read_back(self):
        self.register(fake=fake_client)
        loader = client_loader(self.modules)
        log = {"l": [1, [2]], "t": (3, [4])}
        first = configured(
            {"fake": {"implementation": FAKE_CLASS, "log": log}}, class_loader=loader
        )
        given = first._config()
        again = harnesses.Threaded.configured(
            clients=given.registry.settings.clients, hosts=given.hosts, class_loader=loader
        )
        for cls in (first, again):
            with self.subTest(cls=cls):
                found = registry_of(cls).client("fake").config["log"]
                self.assertEqual(found, log)
                self.assertEqual([type(found[k]) for k in ("l", "t")], [list, tuple])
                self.assertEqual(type(found["t"][1]), list)

    def test_refuses_bad_input_and_keeps_what_was_configured(self):
        self.configure(FAKE)
        tables = "clients: want a mapping of [clients.<name>] tables"

        def build(clients: Any = None, hosts: Any = HOSTS, default_host: Any = "tmux"):
            return harnesses.Threaded.configured(
                clients={} if clients is None else clients,
                hosts=Host.parse_hosts({**hosts, "default": default_host}),
            )

        for kw, message in (
            ({"clients": 1}, tables),
            ({"clients": ["fake"]}, tables),
            ({"clients": {"fake": 1}}, tables),
            ({"clients": {1: {}}}, tables),
            ({"hosts": {**HOSTS, 2: {}}}, "[hosts] 2: want a string key"),
            ({"hosts": {"tmux": 1}}, "[hosts] tmux: want default or a [hosts.<name>] table"),
            (
                {"hosts": {"tmux": {"implementation": "ensemble.TmuxHost"}}},
                "[hosts.tmux].implementation: want module.path:Class",
            ),
            (
                {"default_host": "ghost"},
                "[hosts] default: 'ghost' is not a Host here; want one of tmux",
            ),
            (
                {"default_host": None},
                "[hosts] default: want one of tmux",
            ),
        ):
            with self.subTest(kw=kw):
                self.refused(message, lambda *, kw=kw: build(**kw))
                self.assertEqual(registry_of(self.session_cls).configured(), ["fake"])

    def test_the_host_loads_lazily_when_its_display_settings_are_requested(self):
        with (
            mock.patch.object(importlib, "import_module", wraps=importlib.import_module) as imp,
        ):
            self.session_cls = configured({}, {"status_line": True})
            imp.assert_not_called()
            for _ in range(2):
                self.assertEqual(tui(self.session_cls), TuiSettings(status_line=True))
        imp.assert_called_once_with("ensemble.session.hosts.tmux")
        self.assertIs(
            load_class(HOSTS["tmux"]["implementation"], Host), ensemble.session.hosts.tmux.TmuxHost
        )

    def test_a_registry_keeps_the_parsed_hosts_tables_as_they_are(self):
        source = {"x": {"per_column": [1]}}
        hosts = Hosts("x", source)
        found = registry.Registry(clients={}, hosts=hosts)
        source["x"]["per_column"].append(2)
        source["y"] = {}
        self.assertIs(found.settings.hosts, hosts.tables)
        self.assertEqual(found.settings.hosts, {"x": {"per_column": (1,)}})


class SettingsTest(Base):
    TWO = '[hosts]\ndefault = "a"\n[hosts.a]\nimplementation = "pkg.mod:AHost"\n[hosts.b]\nimplementation = "p.b:B"\n'

    def test_the_clients_hosts_and_default_of_a_read_config(self):
        config = tomllib.loads(self.TWO + FAKE)
        found = registry.settings(config)
        self.assertEqual(
            found,
            {
                "clients": {"fake": {"implementation": FAKE_CLASS}},
                "hosts": Hosts(
                    "a",
                    {"a": {"implementation": "pkg.mod:AHost"}, "b": {"implementation": "p.b:B"}},
                ),
                "viewer": {},
            },
        )
        config["viewer"] = {"grid_retile": "off"}
        self.assertEqual(registry.settings(config)["viewer"], {"grid_retile": "off"})

    def test_hosts_parsed_already_are_not_parsed_again(self):
        hosts = Hosts("x", {"x": {}})
        self.assertEqual(
            registry.settings(tomllib.loads(FAKE), hosts=hosts),
            {"clients": {"fake": {"implementation": FAKE_CLASS}}, "hosts": hosts, "viewer": {}},
        )

    def test_configure_from_a_read_config(self):
        self.register(fake=fake_client)
        own = type("Own", (HostedSession,), {})
        own.configure_from(tomllib.loads(self.TWO + FAKE))
        self.assertEqual(registry_of(own).configured(), ["fake"])
        with self.assertRaisesRegex(ConfigError, r"\A\[hosts\]: missing\Z"):
            own.configure_from({})
        self.assertEqual(registry_of(own).configured(), ["fake"])
        self.assertEqual(registry.settings(tomllib.loads(self.TWO))["clients"], {})
        with self.assertRaisesRegex(ConfigError, r"\A\[hosts\]: missing\Z"):
            registry.settings({})
        for config in (None, 1, [], "x"):
            with (
                self.subTest(config=config),
                self.assertRaisesRegex(ConfigError, r"\Asession config: want a table\Z"),
            ):
                registry.settings(config)

    def test_the_session_config_with_the_local_file_on_top(self):
        with open(os.path.join(self.ensemble, "config.local.toml"), "w") as f:
            f.write(
                "[session.hosts.tmux]\nstatus_line = true\n"
                "[session.clients.opencode]\nmax_runs = 1\n"
            )
        found = registry.settings(read_config())
        self.assertEqual(sorted(found["clients"]), ["claude", "codex", "opencode"])
        self.assertEqual(found["hosts"].default, "tmux")
        self.assertEqual(found["viewer"], {"workers_per_column": 3, "grid_retile": "open-close"})
        self.session_cls = harnesses.Threaded.configured(**found)
        self.assertEqual(tui(self.session_cls), TuiSettings(status_line=True))
        self.assertEqual(registry_of(self.session_cls).max_runs("opencode"), 1)


class TuiTest(Base):
    def test_defaults_when_unset(self):
        self.configure(FAKE)
        self.assertEqual(
            tui(self.session_cls),
            TuiSettings(status_line=False),
        )
        self.assertEqual(
            TuiSettings(),
            TuiSettings(status_line=False),
        )
        self.session_cls = harnesses.Threaded.configured(
            clients={}, hosts=Host.parse_hosts({"other": HOSTS["tmux"], "default": "other"})
        )
        self.assertEqual(tui(self.session_cls), TuiSettings())

    def test_the_hosts_tmux_keys_values(self):
        for line in (True, False):
            with self.subTest(line=line):
                self.configure(FAKE, {"status_line": line})
                self.assertEqual(tui(self.session_cls), TuiSettings(line))

    def test_the_viewers_keys_in_hosts_tmux_are_unknown(self):
        for key, value in (("workers_per_column", 2), ("grid_retile", "all")):
            with self.subTest(key=key):
                self.configure(FAKE, {key: value})
                self.refused(f"unknown key {key!r} in [hosts.tmux]", lambda: tui(self.session_cls))

    def test_a_clients_key_is_no_setting(self):
        self.configure("[clients.fake]\nstatus_line = true\nworkers_per_column = 2\n")
        self.assertEqual(tui(self.session_cls), TuiSettings())

    def test_a_value_of_the_wrong_type_or_range_is_a_config_error_naming_its_key(self):
        self.register(fake=fake_client)
        for key, values, message in (
            ("status_line", ("true", 1, 0, [True]), "status_line: want true or false"),
        ):
            for value in values:
                with self.subTest(key=key, value=value):
                    self.configure(FAKE, {key: value})
                    self.refused(message, lambda: tui(self.session_cls))
                    self.assertIs(
                        type(registry_of(self.session_cls).client("fake")),
                        fake_client.FakeClient,
                    )

    def test_an_unknown_hosts_tmux_key_is_a_config_error_naming_it(self):
        self.configure(FAKE, {"no_such_key": 2})
        self.refused("unknown key 'no_such_key' in [hosts.tmux]", lambda: tui(self.session_cls))


class CountedClient(fake_client.FakeClient):
    built: list = []

    def __init__(self, config):
        type(self).built.append(config)
        super().__init__(config)


class ClientTest(Base):
    def setUp(self):
        CountedClient.built = []

    def test_builds_the_configured_class_from_its_table_minus_implementation(self):
        self.register(fake=fake_client)
        self.configure(FAKE + 'scenario = "/s.json"\nlog = "/log"\n')
        built = registry_of(self.session_cls).client("fake")
        self.assertIs(type(built), fake_client.FakeClient)
        self.assertEqual(built.config, {"scenario": "/s.json", "log": "/log"})

    def test_a_name_unrelated_to_its_module_and_class_loads_through_load_class(self):
        found = registry.Registry(
            clients={
                "alpha": {"implementation": "tests.session.fake_client:FakeClient", "log": "/l"},
                "mine": {"implementation": "ensemble.session.clients.claude:ClaudeClient"},
            },
            hosts=Host.parse_hosts({**HOSTS, "default": "tmux"}),
        )
        self.assertIs(found.class_loader, load_class)
        self.assertIs(found.client_class("alpha"), fake_client.FakeClient)
        self.assertEqual(found.client("alpha").config, {"log": "/l"})
        self.assertIs(type(found.client("mine")), ClaudeClient)

    def test_client_class_loads_without_building_and_client_builds_one_per_call(self):
        self.register(counted=package("counted", CountedClient=CountedClient))
        self.configure(
            '[clients.counted]\nimplementation = "ensemble.session.clients.counted:CountedClient"\n'
            "[clients.counted.log]\na = [1]\n"
        )
        self.assertIs(registry_of(self.session_cls).client_class("counted"), CountedClient)
        self.assertEqual(CountedClient.built, [])
        first, second = (registry_of(self.session_cls).client("counted") for _ in range(2))
        self.assertIsNot(first, second)
        self.assertEqual(CountedClient.built, [{"log": {"a": [1]}}] * 2)
        self.assertIsNot(first.config["log"], second.config["log"])

    def test_refuses_an_unsafe_name(self):
        for bad in ("Fake", "", "../fake", "a-b", "1a", "fake\n", "clients.fake", 1, None):
            for call in (
                registry_of(self.session_cls).client,
                registry_of(self.session_cls).client_class,
            ):
                with self.subTest(bad=bad, call=call.__name__):
                    self.refused(
                        f"unsafe client name {bad!r}: want a lowercase identifier",
                        lambda *, bad=bad, call=call: call(bad),
                    )

    def test_refuses_a_missing_table(self):
        self.configure(
            '[clients.other]\nimplementation = "ensemble.session.clients.other:OtherClient"\n'
        )
        for call in (
            registry_of(self.session_cls).client,
            registry_of(self.session_cls).client_class,
        ):
            with self.subTest(call=call.__name__):
                self.refused(
                    "no [clients.fake] table in the configured clients",
                    lambda *, call=call: call("fake"),
                )

    def test_refuses_an_implementation_not_of_the_module_path_class_form(self):
        self.register(fake=fake_client)
        for line, shown in (
            ("", "None"),
            ("implementation = 1\n", "1"),
            ('implementation = "ensemble.session.clients.fake.FakeClient"\n', None),
            ('implementation = "fake:"\n', None),
            ('implementation = ":FakeClient"\n', None),
            ('implementation = "a:b:FakeClient"\n', None),
            ('implementation = "ensemble.session.clients.fake:FakeClient\\n"\n', None),
            ('implementation = "ensemble..fake:FakeClient"\n', None),
        ):
            with self.subTest(line=line):
                self.configure("[clients.fake]\n" + line)
                path = shown or tomllib.loads(line)["implementation"]
                self.refused(
                    f"[clients.fake].implementation: {path}: want module.path:Class",
                    lambda: registry_of(self.session_cls).client("fake"),
                )

    def test_refuses_the_old_relative_form_naming_the_full_form_before_any_import(self):
        def unused(path, _base):
            raise AssertionError(f"loaded {path}")

        for name, path in (
            ("fake", "clients.fake:FakeClient"),
            ("mine", "clients.claude:ClaudeClient"),
            ("x", "clients.x.y:Z"),
        ):
            with self.subTest(path=path):
                self.session_cls = configured({name: {"implementation": path}}, class_loader=unused)
                self.refused(
                    f"[clients.{name}].implementation: {path} is relative: want "
                    f"ensemble.session.{path}",
                    lambda *, name=name: registry_of(self.session_cls).client_class(name),
                )

    def test_refuses_a_missing_package(self):
        self.configure(
            '[clients.nope]\nimplementation = "ensemble.session.clients.nope:NopeClient"\n'
        )
        self.refused(
            MISSING.format(n="nope", c="Nope"),
            lambda: registry_of(self.session_cls).client("nope"),
        )

    def test_refuses_a_package_that_fails_to_import(self):
        self.configure(
            '[clients.broken]\nimplementation = "ensemble.session.clients.broken:BrokenClient"\n'
        )
        for source, shown in (
            ("import ensemble_absent_dependency\n", "No module named 'ensemble_absent_dependency'"),
            ("from ensemble.session.clients.base import Absent\n", "cannot import name 'Absent'"),
            ("raise OSError('disk gone')\n", "disk gone"),
            ("def broken(:\n", "invalid syntax"),
        ):
            with (
                self.subTest(source=source),
                mock.patch.object(
                    sys,
                    "meta_path",
                    [SourceFinder("ensemble.session.clients.broken", source), *sys.meta_path],
                ),
            ):
                with self.assertRaises(ConfigError) as caught:
                    registry_of(self.session_cls).client("broken")
                self.assertTrue(
                    str(caught.exception).startswith(
                        "[clients.broken].implementation: ensemble.session.clients.broken:BrokenClient: "
                        f"cannot import ensemble.session.clients.broken: {shown}"
                    ),
                    caught.exception,
                )

    def test_refuses_a_missing_class(self):
        self.register(empty=package("empty"))
        self.configure(
            '[clients.empty]\nimplementation = "ensemble.session.clients.empty:EmptyClient"\n'
        )
        self.refused(
            (
                "[clients.empty].implementation: ensemble.session.clients.empty:EmptyClient: "
                "ensemble.session.clients.empty has no EmptyClient"
            ),
            lambda: registry_of(self.session_cls).client("empty"),
        )

    def test_refuses_what_is_not_a_concrete_client_subclass(self):
        for value in (OddClient, odd_client, Client, AbstractClient, fake_client.FakeHeadless):
            with self.subTest(value=value):
                self.register(odd=package("odd", OddClient=value))
                self.configure(
                    '[clients.odd]\nimplementation = "ensemble.session.clients.odd:OddClient"\n'
                )
                self.refused(
                    (
                        "[clients.odd].implementation: ensemble.session.clients.odd:OddClient "
                        "must be a concrete Client subclass"
                    ),
                    lambda: registry_of(self.session_cls).client_class("odd"),
                )

    def test_a_client_offering_one_mode_is_concrete(self):
        self.register(one=package("one", OneClient=HeadlessOnlyClient))
        self.configure('[clients.one]\nimplementation = "ensemble.session.clients.one:OneClient"\n')
        self.assertIs(registry_of(self.session_cls).client_class("one"), HeadlessOnlyClient)

    def test_an_unknown_key_is_the_clients_config_error(self):
        self.register(fake=fake_client)
        self.configure(FAKE + 'model = "m"\nscenario = "/s"\nalpha = 1\n')
        self.refused(
            "unknown key 'alpha' in FakeClient's config",
            lambda: registry_of(self.session_cls).client("fake"),
        )


PLUGIN_OFF = '{"enabledPlugins": {"ensemble@windroc": false}}'


def committed() -> dict:
    return registry.settings(read_config(local=os.devnull))["clients"]


class Committed(Case):
    def test_the_committed_clients_configure_agent_clients(self):
        self.session_cls = configured(committed())
        self.assertEqual(
            registry_of(self.session_cls).configured(), ["claude", "codex", "opencode"]
        )
        for name, table in committed().items():
            with self.subTest(name=name):
                self.assertEqual({"roles", "output"} & set(table), set())

    def test_the_claude_table_builds_a_claude_client(self):
        tmp = scratch(self, real=False)
        patch = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": os.path.join(tmp, "config")})
        patch.start()
        self.addCleanup(patch.stop)
        tables = committed()
        self.assertEqual(
            tables["claude"],
            {
                "implementation": "ensemble.session.clients.claude:ClaudeClient",
                "max_runs": 0,
                "flags": ["--settings", PLUGIN_OFF],
                "allow": [],
                "account": "",
                "local_account": "",
                "modes": {
                    "auto": ["--permission-mode", "auto"],
                    "yolo": [
                        "--dangerously-skip-permissions",
                        "--settings",
                        '{"skipDangerousModePermissionPrompt": true}',
                    ],
                },
                "env": {},
                "accounts": {},
                "tiers": {"1": "fable", "2": "opus", "3": "sonnet", "4": "haiku"},
                "efforts": {e: e for e in ("low", "medium", "high", "xhigh", "max")},
            },
        )
        self.session_cls = configured(tables)
        client = registry_of(self.session_cls).client("claude")
        self.assertIs(type(client), ClaudeClient)
        argv = (
            client.headless()
            .launch(
                Spec(
                    workdir=tmp,
                    access=Access(cwd=tmp),
                    tier=4,
                    effort="low",
                    sid=SID,
                    prompt="hi",
                )
            )
            .argv
        )
        self.assertEqual(
            argv[4:12],
            (
                "--model",
                "haiku",
                "--effort",
                "low",
                *("--permission-mode", "auto"),
                "--setting-sources",
                "user",
            ),
        )

    def test_every_committed_claude_session_turns_the_ensemble_plugin_off(self):
        tmp = scratch(self, real=False)
        self.enterContext(
            mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": os.path.join(tmp, "config")})
        )
        client = registry_of(configured(committed())).client("claude")
        for flags in ((), ("--settings", '{"model": "x"}')):
            spec = Spec(workdir=tmp, access=Access(cwd=tmp), sid=SID, prompt="hi", flags=flags)
            for launch in (client.headless().launch(spec), client.tui().launch(spec, ("h",))):
                with self.subTest(flags=flags, argv=launch.argv[1]):
                    argv = launch.argv
                    self.assertEqual(argv.count("--settings"), 1)
                    settings = json.loads(argv[argv.index("--settings") + 1])
                    self.assertEqual(settings["enabledPlugins"], {"ensemble@windroc": False})


if __name__ == "__main__":
    unittest.main()
