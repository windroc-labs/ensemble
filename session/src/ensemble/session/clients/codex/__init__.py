"""Codex's abstract factory."""

from collections.abc import Mapping
from types import MappingProxyType

from ensemble.session.clients.base import Client
from ensemble.session.clients.codex.common import CodexCommon
from ensemble.session.clients.codex.headless import CodexHeadless
from ensemble.session.clients.codex.tui import CodexTui
from ensemble.session.engine.types import SessionInfo, Spec
from ensemble.utils import LOCAL_ENV_VARS


class CodexClient(Client):
    selection = ((), ("resume",))
    valued_flags = (
        "-c",
        "--config",
        "-C",
        "--cd",
        "-p",
        "--profile",
        "--enable",
        "--disable",
        "--add-dir",
        "-m",
        "--model",
        "-s",
    )
    forbidden_selection = ("resume", "fork", "--last")
    keys = frozenset({"flags", "tiers", "efforts", "env", "modes"})
    client_note = "Run each shell call separately."
    session_env = "CODEX_THREAD_ID"
    identity_env = tuple(
        key for key in CodexCommon.UNSET if key != "CLAUDECODE" and key not in LOCAL_ENV_VARS
    )
    child_defaults = CodexCommon.DEFAULTS

    def __init__(self, config):
        super().__init__(config)
        self.common = CodexCommon(self.config, selector=self.select_session)

    def headless(self):
        return CodexHeadless(self.common)

    def tui(self):
        return CodexTui(self.common)

    def usage_probe(self):
        return self.common.probe()

    def _usage_executable(self, *, container=False):
        return "codex"

    def usage(self, output):
        return self.common.parse_usage(output)

    def session_metadata(self, info):
        return self.common.session_metadata(info)

    def resume_main(self, argv):
        return self.common.resume_main(argv)

    def session_status(self, info: SessionInfo, *, env: Mapping[str, str] = MappingProxyType({})):
        return self.common.session_status(info.sid, info.cwd, transcript=info.transcript)

    def writable_dirs(self, spec: Spec):
        return self.common.writable_dirs(spec)
