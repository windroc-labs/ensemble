"""src/ensemble/delegate/config.py: its resources; delegates(), a read delegate config's
[delegates], which takes no keys; policy()'s shape (plan's Policy cases: through plan) and
trusted_dirs().
"""

import json
import os
import re
import tomllib
import unittest
from unittest import mock

from ensemble.delegate.composer.run import check_old_keys
from ensemble.delegate.config import (
    CONFIG,
    delegates,
    policy,
    trusted_dirs,
)
from ensemble.delegate.engine import config as delegate
from ensemble.delegate.engine.config import read_config
from ensemble.delegate.engine.environment import resolve_fields
from ensemble.session.engine.config import read_config as read_session_config
from tests.delegate import case
from tests.delegate.case import Case
from tests.delegate.unit.composer.shipped import (
    generic_names,
)


def parsed(text: str = "") -> dict:
    return tomllib.loads(text)


class PathsTest(Case):
    def test_public_locations_cover_only_the_delegate_and_its_lower_layers_in_order(self):
        roots = (os.path.join(case.REPO, "src"),)
        self.assertEqual(delegate.module_roots(), roots)

    def test_config_is_a_resource_inside_the_delegate_package(self):
        self.assertEqual(
            CONFIG,
            os.path.join(case.REPO, "src", "ensemble", "delegate", "config.toml"),
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
        self.assertEqual(found["delegates"], {})
        with open(CONFIG, "rb") as f:
            self.assertEqual({"hosts", "clients"} & set(tomllib.load(f)), set())


class DelegatesTest(Case):
    def refused(self, config, message):
        with self.assertRaises(ValueError) as caught:
            delegates(config)
        self.assertEqual(str(caught.exception), message)

    def test_an_absent_or_empty_table_passes(self):
        for text in ("", "[delegates]\n"):
            with self.subTest(text):
                self.assertIsNone(delegates(parsed(text)))

    def test_the_committed_config_passes(self):
        self.assertIsNone(delegates(read_config()))

    def test_harness_host_refused(self):
        for value in ('""', '"tmux"'):
            with self.subTest(value=value):
                self.refused(
                    parsed(f"[delegates]\nharness_host = {value}\n"),
                    "[delegates] harness_host: the delegate takes no keys",
                )

    def test_delegates_takes_no_keys(self):
        self.refused(
            parsed("[delegates]\nkind = 1\n"), "[delegates] kind: the delegate takes no keys"
        )
        self.refused(
            parsed("[delegates.linear]\nx = 1\n"),
            "[delegates] linear: the delegate takes no keys",
        )
        self.refused({"delegates": 1}, "[delegates]: the delegate takes no keys")


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


D2 = """
[session.hosts.d2]
implementation = "ensemble.session.hosts.docker.host:DockerHost"
"""
DUMMY = """
[delegate.environments.dummy]
image = "dummy-env:test"
plugins = ["dummy-plugin"]
lock = "dummy-lock"

[[delegate.environments.dummy.credentials.env]]
item = "ensemble-broker-env/dummy"
file = "dummy.txt"
"""


class EnvironmentCheckTest(Case):
    """read_config's check_environments: each refusal names its table."""

    def loaded(self, text: str) -> dict:
        path = os.path.join(self.home, "config.local.toml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return read_config(local=path)

    def refused(self, text: str, *parts: str) -> str:
        with self.assertRaises(ValueError) as caught:
            self.loaded(text)
        for part in parts:
            self.assertIn(part, str(caught.exception))
        return str(caught.exception)

    def test_the_packaged_default_is_the_phase_1_worker(self):
        found = read_config(local=os.devnull)
        self.assertEqual(
            found["environment"]["docker"],
            {
                "image": "@worker",
                "gateway": "workers",
                "plugins": [],
                "prefetch": False,
            },
        )
        self.assertEqual(list(found["environments"]), ["mini-op"])

    def test_the_packaged_mini_op_environment_holds_only_its_published_names(self):
        found = read_config(local=os.devnull)
        self.assertEqual(
            found["environments"]["mini-op"],
            {
                "image": "mini-op:env",
                "gateway": "mini-op",
                "plugins": [],
                "lock": "mini-op",
                "credentials": {
                    "env": [
                        {"item": "ensemble-broker-env/mini-op-password", "file": "mini-password"},
                        {"item": "ensemble-broker-env/mini-op-ssh-key", "file": "mini-ssh-key"},
                    ]
                },
            },
        )
        spec = resolve_fields(
            found,
            host="docker",
            name="mini-op",
            overrides={},
            packaged=delegate.packaged_environments(),
        )
        assert spec is not None
        self.assertFalse(spec.prefetch)
        self.assertEqual(spec.spec.image, "mini-op:env")
        self.assertEqual(
            found["roles"]["verifier"]["environment"], {"name": "mini-op", "prefetch": True}
        )

    def test_a_local_named_environment_and_role_table_load(self):
        found = self.loaded(
            DUMMY + '[delegate.roles.dummy-tester.environment]\nname = "dummy"\nprefetch = true\n'
        )
        self.assertEqual(found["roles"]["dummy-tester"]["environment"]["name"], "dummy")

    def test_local_credential_values_are_preserved_whole(self):
        for source, expected in (("{}", {}), ("{ future_kind = {} }", {"future_kind": {}})):
            for table in (
                "delegate.environment.docker",
                "delegate.environments.mini-op",
                "delegate.roles.dummy-tester.environment",
            ):
                with self.subTest(table=table, source=source):
                    found = self.loaded(
                        f"[{table}]\ncredentials = {source}\n"
                        + (
                            'name = "mini-op"\n'
                            if table == "delegate.roles.dummy-tester.environment"
                            else ""
                        )
                    )
                    overrides = dict(
                        found.get("roles", {}).get("dummy-tester", {}).get("environment", {})
                    )
                    name = overrides.pop(
                        "name", "mini-op" if table == "delegate.environments.mini-op" else None
                    )
                    resolved = resolve_fields(
                        found,
                        host="docker",
                        name=name,
                        overrides=overrides,
                        packaged=delegate.packaged_environments(),
                    )
                    assert resolved is not None
                    self.assertEqual(json.loads(resolved.credentials), expected)

    def test_unknown_keys_are_refused_naming_the_table(self):
        self.refused(
            "[delegate.environments.mini-op]\nsecrets = []",
            "unknown key 'secrets'",
            "[environments.mini-op]",
        )
        for text, table, key in (
            ('[delegate.environment.docker]\nports = ["80"]\n', "[environment.docker]", "ports"),
            ('[delegate.environments.dummy]\nmounts = ["/x"]\n', "[environments.dummy]", "mounts"),
            (
                DUMMY + '[delegate.roles.dummy-tester.environment]\npassword = "p"\n',
                "[roles.dummy-tester.environment]",
                "password",
            ),
            (
                '[[delegate.environments.e.secrets]]\nitem = "ensemble-broker-env/e"\nfile = "f"\n'
                'value = "v"\n',
                "[environments.e]",
                "secrets",
            ),
        ):
            with self.subTest(table):
                self.refused(text, table, f"unknown key {key!r}")

    def test_a_default_table_for_a_host_that_is_not_docker_kind(self):
        self.refused(
            '[delegate.environment.tmux]\ngateway = "workers"\n',
            "[environment.tmux]",
            "[delegate.environment.tmux]",
            "not a Docker-kind Host",
        )
        self.refused('[delegate.environment.nowhere]\nimage = "x"\n', "[environment.nowhere]")

    def test_a_docker_kind_default_missing_image_and_gateway(self):
        self.refused(
            D2 + '[delegate.environment.d2]\nlock = "l"\n', "[environment.d2]", "image", "gateway"
        )

    def test_a_docker_kind_host_needs_no_default_table(self):
        self.assertNotIn("d2", self.loaded(D2)["environment"])

    def test_a_role_naming_an_unknown_environment(self):
        self.refused(
            '[delegate.roles.dummy-tester.environment]\nname = "nope"\n',
            "[roles.dummy-tester.environment]",
            "[environments.nope]",
        )

    def test_name_only_in_a_role_table(self):
        self.refused('[delegate.environments.e]\nname = "x"\n', "[environments.e]", "name")

    def test_environment_names_follow_the_lock_name_rule(self):
        self.refused('[delegate.environments.Bad]\nimage = "x"\n', "[environments.Bad]")
        self.refused(
            '[delegate.roles.dummy-tester.environment]\nname = "-x"\n',
            "[roles.dummy-tester.environment]",
        )

    def test_invalid_values_are_refused_naming_the_table(self):
        for text, table in (
            ('[delegate.environments.e]\nprefetch = "yes"\n', "[environments.e]"),
            ('[delegate.environments.e]\nlock = "A B"\n', "[environments.e]"),
            ('[delegate.environments.e]\nplugins = "x"\n', "[environments.e]"),
            ('[delegate.environment.docker]\ngateway = "No Such"\n', "[environment.docker]"),
            (
                "[delegate.roles.dummy-tester]\nenvironment = 3\n",
                "[roles.dummy-tester.environment]",
            ),
        ):
            with self.subTest(text):
                self.refused(text, table)


if __name__ == "__main__":
    unittest.main()
