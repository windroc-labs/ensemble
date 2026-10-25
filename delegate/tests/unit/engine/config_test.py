"""delegate/src/ensemble/delegate/config.py: its resources; harness_host(), a read delegate
config's [delegates] harness_host checked against Host.parse_hosts' names (their cases:
session/tests/unit/hosts/base_test.py); policy()'s shape (plan's Policy cases: through plan) and
trusted_dirs().
"""

import os
import re
import tomllib
import unittest
from unittest import mock

from ensemble.delegate.composer.run import check_old_keys
from ensemble.delegate.config import (
    CONFIG,
    harness_host,
    policy,
    trusted_dirs,
)
from ensemble.delegate.engine import config as delegate
from ensemble.delegate.engine.config import read_config
from ensemble.session import Host
from ensemble.session.engine.config import read_config as read_session_config

from delegate.tests import case
from delegate.tests.case import Case
from delegate.tests.unit.composer.shipped import (
    generic_names,
)

TWO = """[hosts]
default = "a"

[hosts.a]
implementation = "pkg.mod:AHost"
per_column = 2

[hosts.b_2]
implementation = "pkg.b:BHost"
"""
NAMES = "a, b_2"
HOSTS = Host.parse_hosts(tomllib.loads(TWO)["hosts"]).tables


def parsed(text: str = "") -> dict:
    return tomllib.loads(text)


class PathsTest(Case):
    def test_public_locations_cover_only_the_delegate_and_its_lower_layers_in_order(self):
        roots = tuple(
            os.path.join(case.REPO, name, "src") for name in ("delegate", "session", "utils")
        )
        self.assertEqual(delegate.module_roots(), roots)

    def test_config_is_a_resource_inside_the_delegate_package(self):
        self.assertEqual(
            CONFIG,
            os.path.join(case.REPO, "delegate", "src", "ensemble", "delegate", "config.toml"),
        )
        self.assertTrue(os.path.isfile(CONFIG))

    def test_defaults_read_outside_the_repository_with_lower_layer_settings(self):
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.home)
        found = read_config(local=os.devnull)
        with open(CONFIG, "rb") as stream:
            own = tomllib.load(stream)
        self.assertEqual(found["language"], own["language"])
        self.assertEqual(found["hosts"], read_session_config(local=os.devnull)["hosts"])

    def test_the_delegate_reads_no_hosts_or_clients(self):
        found = read_config()
        self.assertEqual(found["delegates"], {"harness_host": ""})
        with open(CONFIG, "rb") as f:
            self.assertEqual({"hosts", "clients"} & set(tomllib.load(f)), set())


class HarnessHostTest(Case):
    def refused(self, config, message):
        with self.assertRaises(ValueError) as caught:
            harness_host(config, HOSTS)
        self.assertEqual(str(caught.exception), message)

    def test_unset_is_none(self):
        for text in ("", "[delegates]\n", '[delegates]\nharness_host = ""\n'):
            with self.subTest(text):
                self.assertIsNone(harness_host(parsed(text), HOSTS))

    def test_harness_host(self):
        self.assertEqual(harness_host(parsed('[delegates]\nharness_host = "b_2"\n'), HOSTS), "b_2")

    def test_the_committed_config_has_no_harness_host(self):
        hosts = Host.parse_hosts(read_session_config()["hosts"]).tables
        self.assertIsNone(harness_host(read_config(), hosts))

    def test_delegates_takes_only_harness_host(self):
        self.refused(
            parsed("[delegates]\nkind = 1\n"),
            "[delegates] kind: the delegate takes only harness_host",
        )
        self.refused(
            parsed('[delegates]\nharness_host = "a"\n[delegates.linear]\nx = 1\n'),
            "[delegates] linear: the delegate takes only harness_host",
        )
        self.refused({"delegates": 1}, "[delegates]: the delegate takes only harness_host")

    def test_harness_host_must_name_a_table(self):
        for value in ('"c"', "1", '"default"', "[1]"):
            with self.subTest(value=value):
                self.refused(
                    parsed(f"[delegates]\nharness_host = {value}\n"),
                    f"[delegates] harness_host: want one of {NAMES}",
                )


class PolicyTest(Case):
    def test_a_clients_flags_and_env_else_none(self):
        cfg = parsed('[policy.codex]\nflags = ["-s"]\nenv = { K = "v" }\n')
        self.assertEqual(policy("codex", cfg), (["-s"], {"K": "v"}))
        self.assertEqual(policy("fake", cfg), ([], {}))

    def test_a_malformed_table_is_a_value_error(self):
        for cfg in (
            {"policy": 1},
            {"policy": {"c": 1}},
            {"policy": {"c": {"x": 1}}},
            {"policy": {"c": {"flags": "-s"}}},
        ):
            with self.subTest(cfg=cfg), self.assertRaises(ValueError):
                policy("c", cfg)


class TrustedDirsTest(Case):
    def test_unset_is_empty_and_paths_are_real_paths(self):
        self.assertEqual(trusted_dirs({}), frozenset())
        with mock.patch.dict(os.environ, {"HOME": "/var/h"}):
            got = trusted_dirs({"trusted_dirs": ["~/c", "/x/../y"]})
        self.assertEqual(got, {os.path.realpath("/var/h/c"), os.path.realpath("/y")})


class Shipped(Case):
    def test_team_dirs_may_come_only_from_the_local_section(self):
        own = os.path.join(self.home, "delegate.toml")
        local = os.path.join(self.home, "local.toml")
        with open(own, "w") as stream:
            stream.write('team_dirs = ["/untrusted"]\n')
        with open(local, "w") as stream:
            stream.write("[delegate]\nteam_dirs = []\n")
        with self.assertRaises(ValueError) as caught:
            read_config(path=own, local=local)
        self.assertEqual(str(caught.exception), f"team_dirs: set it in {local}, not delegate.toml")
        with open(own, "w") as stream:
            stream.write("team_dirs = []\n")
        with open(local, "w") as stream:
            stream.write(f'[delegate]\nteam_dirs = ["{self.home}"]\n')
        self.assertEqual(read_config(path=own, local=local)["team_dirs"], [self.home])

    def test_public_config_reader_supports_local_and_path_overrides(self):
        from_config = os.path.join(self.home, "delegate.toml")
        local = os.path.join(self.home, "local.toml")
        with open(from_config, "w") as stream:
            stream.write('language = "base"\n')
        with open(local, "w") as stream:
            stream.write('[delegate]\nlanguage = "local"\n')
        found = delegate.read_config(local=local, path=from_config)
        self.assertEqual(found["language"], "local")
        self.assertIn("hosts", found)

    def test_config_comments_name_skills_by_slash_command(self):
        with open(CONFIG) as f:
            self.assertEqual(generic_names("\n".join(re.findall(r"#.*", f.read()))), [])

    def test_local_lines_uncommented_are_a_valid_local_file(self):
        with open(CONFIG) as f:
            local = tomllib.loads(
                "".join(
                    line.removeprefix("# local: ") for line in f if line.startswith("# local: ")
                )
            )["delegate"]
        self.assertLessEqual({"language", "cwd", "users", "trusted_dirs", "team_dirs"}, set(local))
        self.assertLessEqual({"researcher", "pm"}, set(local["roles"]))
        for role, table in local["roles"].items():
            check_old_keys(table, role)


if __name__ == "__main__":
    unittest.main()
