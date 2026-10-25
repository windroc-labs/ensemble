"""Claude Code's Client, client `claude`: ClaudeHeadless (headless.py), ClaudeTui (tui.py); the
rest is ClaudeCommon's.
"""

import os
from collections.abc import Mapping
from types import MappingProxyType

from ensemble.session.clients.base import Client
from ensemble.session.clients.claude.common import ClaudeCommon, added_dirs
from ensemble.session.clients.claude.headless import ClaudeHeadless
from ensemble.session.clients.claude.tui import ClaudeTui
from ensemble.session.engine.types import SessionInfo, Spec
from ensemble.utils import LOCAL_ENV_VARS


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
    keys = frozenset({"flags", "allow", "tiers", "efforts", "env", "modes", "accounts"})
    client_note = ClaudeCommon.NOTE
    session_env = "CLAUDECODE"
    identity_env = tuple(key for key in ClaudeCommon.UNSET if key not in LOCAL_ENV_VARS)

    def __init__(self, config):
        super().__init__(config)
        self.common = ClaudeCommon(self.config, selector=self.select_session)

    def headless(self):
        return ClaudeHeadless(self.common)

    def tui(self):
        return ClaudeTui(self.common)

    def _accounts(self):
        return frozenset(self.common.accounts)

    def usage_probe(self):
        return self.common.probe()

    def _usage_executable(self, *, container=False):
        return "claude"

    def _usage_probes(self):
        accounts = self.common.accounts
        return {name: (accounts[name].dir, self.common.probe(name)) for name in accounts}

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
