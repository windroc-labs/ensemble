"""OpenCode's Client, client `opencode`: OpenCodeHeadless (headless.py), OpenCodeTui (tui.py); the
rest is OpenCodeCommon's.
"""

import os
from collections.abc import Mapping
from types import MappingProxyType

from ensemble.session.clients.base import Client
from ensemble.session.clients.opencode.common import IDENTITY, SELECTORS, VALUED, OpenCodeCommon
from ensemble.session.clients.opencode.headless import OpenCodeHeadless
from ensemble.session.clients.opencode.tui import OpenCodeTui
from ensemble.session.engine.types import SessionInfo, Spec


class OpenCodeClient(Client):
    selection = ((), ("--session",))
    valued_flags = VALUED
    forbidden_selection = SELECTORS
    keys = frozenset(
        {
            "flags",
            "modes",
            "tiers",
            "efforts",
            "env",
            "ollama_port",
            "ollama_context",
            "executable",
            "model",
            "providers",
        }
    )
    client_note = (
        "Pre-approved commands match on their exact text and each shell call starts fresh, so anything else is "
        "refused."
    )
    session_env = "OPENCODE"
    identity_env = IDENTITY

    def __init__(self, config):
        super().__init__(config)
        self.common = OpenCodeCommon(self.config, selector=self.select_session)

    def headless(self):
        return OpenCodeHeadless(self.common)

    def tui(self):
        return OpenCodeTui(self.common)

    def usage_probe(self):
        return self.common.probe()

    def _executable(self, *, container=False):
        return "" if container else self.common.executable

    def _provider_probe(self):
        return self.common.usage_provider()

    def usage(self, output):
        return self.common.parse_usage(output)

    def session_status(self, info: SessionInfo, *, env: Mapping[str, str] = MappingProxyType({})):
        return self.common.session_status(info.cwd, info.sid, env, transcript=info.transcript)

    def writable_dirs(self, spec: Spec):
        dirs = (spec.access.cwd, *spec.access.dirs)
        return tuple(dict.fromkeys(map(os.path.realpath, dirs)))
