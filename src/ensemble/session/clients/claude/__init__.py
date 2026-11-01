"""Claude Code's Client, client `claude`: ClaudeHeadless (headless.py), ClaudeTui (tui.py); the
rest is ClaudeCommon's.
"""

import os
from collections.abc import Mapping
from types import MappingProxyType

from ensemble.session.clients.base import Client
from ensemble.session.clients.claude.common import TABLE, ClaudeCommon, added_dirs
from ensemble.session.clients.claude.headless import ClaudeHeadless
from ensemble.session.clients.claude.tui import ClaudeTui
from ensemble.session.engine.types import ConfigError, SessionInfo, Spec
from ensemble.utils import LOCAL_ENV_VARS, printable_abs


class ClaudeClient(Client):
    selection = (("--session-id",), ("--resume",))
    selection_uuid = True
    valued_flags = (
        "--model",
        "--effort",
        "--name",
        "--settings",
        "--add-dir",
        "--setting-sources",
        "--mcp-config",
    )
    forbidden_selection = (
        "--resume",
        "-r",
        "--session-id",
        "--continue",
        "-c",
        "--fork-session",
        "--from-pr",
        "--teleport",
    )
    keys = frozenset(
        {
            "flags",
            "allow",
            "tiers",
            "efforts",
            "env",
            "modes",
            "accounts",
            "account",
            "local_account",
        }
    )
    client_note = ClaudeCommon.NOTE
    session_env = "CLAUDECODE"
    identity_env = tuple(key for key in ClaudeCommon.UNSET if key not in LOCAL_ENV_VARS)

    def __init__(self, config):
        super().__init__(config)
        self.common = ClaudeCommon(self.config, selector=self.select_session)
        for key in ("account", "local_account"):
            account = self.config.get(key, "")
            if not isinstance(account, str):
                raise ConfigError(f"{TABLE}.{key} must be a string")
            if account and account not in self.common.accounts:
                raise ConfigError(
                    f"{TABLE}.{key} {account!r}: not a registered account"
                    f" ([clients.claude.accounts.{account}])"
                )

    def headless(self):
        return ClaudeHeadless(self.common)

    def tui(self):
        return ClaudeTui(self.common)

    def _accounts(self):
        return frozenset(self.common.accounts)

    def _account(self, *, container):
        return self.config.get("account" if container else "local_account", "")

    def _account_env(self, account):
        return {"CLAUDE_CONFIG_DIR": self.common.accounts[account].dir}

    def _account_of(self, transcript):
        if not printable_abs(transcript):
            return ""
        path = os.path.realpath(transcript)
        default = os.path.realpath(self.common.config_dir())
        if os.path.commonpath((path, default)) == default:
            return ""
        for name in sorted(self.common.accounts):
            root = os.path.realpath(self.common.accounts[name].dir)
            if os.path.commonpath((path, root)) == root:
                return name
        return ""

    def _executable(self, *, container=False):
        return "claude"

    def _usage_probes(self):
        return {name: self.common.probe(name) for name in self.common.accounts}

    def _max_usage(self, account):
        return self.common.accounts[account].max_usage

    def usage(self, output):
        return self.common.parse_usage(output)

    def resume_main(self, argv):
        return self.common.resume_main(argv)

    def session_status(self, info: SessionInfo, *, env: Mapping[str, str] = MappingProxyType({})):
        return self.common.session_status(info.sid, info.cwd, transcript=info.transcript, env=env)

    def writable_dirs(self, spec: Spec):
        cwd = spec.access.cwd
        dirs = (cwd, *spec.access.dirs, *(os.path.join(cwd, d) for d in added_dirs(spec.flags)))
        return tuple(dict.fromkeys(map(os.path.realpath, dirs)))
