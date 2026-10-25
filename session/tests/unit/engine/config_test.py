"""The public session configuration reader owns the session local section."""

import os
from pathlib import Path

from ensemble import session
from ensemble.session.engine.config import read_config
from ensemble.session.engine.types import ConfigError
from ensemble.session.hosts.base import Host
from ensemble.utils import read_resources

from session.tests.case import Case


class Configuration(Case):
    def test_public_configuration_resources_match_the_reader(self):
        self.assertEqual(session.CONFIG_RESOURCES, (("ensemble.session", "config.toml"),))
        self.assertEqual(
            read_resources(session.CONFIG_RESOURCES, local=os.devnull),
            read_config(local=os.devnull),
        )

    def test_only_the_session_section_overlays_session_defaults(self):
        local = Path(self.home, "config.local.toml")
        local.write_text(
            "[session.hosts.tmux]\nstatus_line = true\n[delegate]\nunknown = 1\n[orchestrator]\nunknown = 2\n"
        )
        found = read_config(local=str(local))
        self.assertTrue(found["hosts"]["tmux"]["status_line"])
        self.assertIn("clients", found)
        self.assertNotIn("roles", found)

    def test_flat_local_keys_have_a_session_migration_hint(self):
        local = Path(self.home, "config.local.toml")
        local.write_text("[hosts.tmux]\nstatus_line = true\n")
        with self.assertRaisesRegex(
            ValueError, r"unknown key 'hosts.tmux.status_line'.*move it under \[session\]"
        ):
            read_config(local=str(local))
        self.assertIn("hosts", read_config(local=os.devnull))

    def test_a_local_default_host_of_pty_is_refused_naming_the_hosts_left(self):
        local = Path(self.home, "config.local.toml")
        local.write_text('[session.hosts]\ndefault = "pty"\n')
        with self.assertRaises(ConfigError) as caught:
            Host.parse_hosts(read_config(local=str(local))["hosts"])
        self.assertEqual(
            str(caught.exception),
            "[hosts] default: 'pty' is not a Host here; want one of docker, tmux",
        )

    def test_utils_local_keys_are_rejected_as_an_unconfigured_lower_layer(self):
        local = Path(self.home, "config.local.toml")
        local.write_text("[utils]\nunknown = 1\n")
        with self.assertRaisesRegex(ValueError, "unknown key 'unknown' in \\[utils\\]"):
            read_config(local=str(local))
