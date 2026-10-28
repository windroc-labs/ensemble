"""Session behavior tests with caller-owned dependencies."""

import json
import os
import shlex
from dataclasses import replace
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from ensemble.session.clients.claude import ClaudeClient
from ensemble.session.clients.claude.common import ClaudeCommon as UnconfiguredCommon
from ensemble.session.clients.common import select
from ensemble.session.engine.types import (
    LOCAL_CONTEXT,
    Access,
    ConfigError,
    HookKind,
    Launch,
    Permission,
)
from ensemble.session.engine.types import Spec as BaseSpec
from ensemble.utils import LOCAL_ENV_VARS
from tests.session.case import SID, Case
from tests.utils.sandbox import scratch


class ClaudeCommon(UnconfiguredCommon):
    def __init__(self, config):
        super().__init__(
            config,
            selector=lambda native, resume: select(
                native, ClaudeClient.selection, uuid=True, resume=resume
            ),
        )


Spec = BaseSpec

CEILING = {"CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "3600000"}
TUI_DEFAULTS = {"CLAUDE_CODE_DISABLE_AGENT_VIEW": "1"}

UNSET = (
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_JOB_DIR",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_PID",
    "CLAUDE_EFFORT",
    *sorted(LOCAL_ENV_VARS),
)

PROBE = (
    "claude",
    "-p",
    "--no-session-persistence",
    "--strict-mcp-config",
    "--setting-sources",
    "user",
    "--settings",
    '{"remoteControlAtStartup":false,"disableAllHooks":true}',
    "/usage",
)
FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def fixture(name) -> str:
    """A real `claude -p /usage` stdout (Claude Code 2.1.302, Agent_Usage_Monitor's spikes)."""
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


NOTE = (
    "Pre-approved commands match on their exact text and each shell call starts fresh, so anything else waits on a "
    "permission prompt."
)

CONFIG = {
    "flags": ["--permission-mode", "auto"],
    "allow": ["Read", "Edit(/x)"],
    "tiers": {"1": "fable", "4": "haiku"},
    "efforts": {"low": "lo", "max": "mx"},
    "env": {"K": "v"},
}

WHAT = {
    "flags": "a list of strings",
    "allow": "a list of strings",
    "tiers": 'a table from tier "1"–"4" to a model name',
    "efforts": "a table from effort (low, medium, high, xhigh, max) to a printable value",
    "env": "a table of environment names to strings without NUL",
    "modes": "a table from auto and yolo to a list of strings",
}

SKIP_DIALOG = '{"skipDangerousModePermissionPrompt": true}'
MODES = {
    "auto": ["--permission-mode", "auto"],
    "yolo": ["--dangerously-skip-permissions", "--settings", SKIP_DIALOG],
}


class Base(Case):
    def setUp(self):
        self.tmp = self.dir()
        self.config_dir = os.path.join(self.tmp, "config")
        self.env({"CLAUDE_CONFIG_DIR": self.config_dir})
        self.work = os.path.join(self.tmp, "work")
        os.mkdir(self.work)

    def dir(self) -> str:
        return scratch(self)

    def env(self, values):
        patch = mock.patch.dict(os.environ, values)
        patch.start()
        self.addCleanup(patch.stop)

    def refused(self, call, message):
        with self.assertRaises(ConfigError) as caught:
            call()
        self.assertEqual(str(caught.exception), message)

    def launch(
        self, prompt="hi", config=CONFIG, *, resume=False, permission=Permission.AUTO, **spec
    ) -> Launch:
        spec = Spec(
            **{
                "workdir": self.work,
                "access": Access(cwd=self.work),
                "sid": SID,
                "prompt": prompt,
                "resume": resume,
                **spec,
            }
        )
        run = ClaudeClient(config).headless()
        run._context = replace(LOCAL_CONTEXT, permission=permission)
        return run.launch(spec)


def stream_line(record) -> str:
    return json.dumps(record)


def assistant(*blocks, **fields) -> dict:
    return {"type": "assistant", "message": {"content": list(blocks)}, **fields}


def text(value) -> dict:
    return {"type": "text", "text": value}


def tool(name) -> dict:
    return {"type": "tool_use", "id": "t", "name": name, "input": {}}


def rejected(resets=1700000000, status="rejected") -> dict:
    return {"type": "rate_limit_event", "rate_limit_info": {"status": status, "resetsAt": resets}}


INIT = {"type": "system", "subtype": "init", "session_id": SID}


HOOK = ("/py thon", "-I", "/core/tui_hook.py", "/tui/w 1")

HOOKS = {
    "Stop": HookKind.TURN_END,
    "StopFailure": HookKind.TURN_FAILED,
    "PermissionRequest": HookKind.BLOCKED,
    "Notification": HookKind.BLOCKED,
    "PostToolUse": HookKind.ACTIVITY,
    "UserPromptSubmit": HookKind.TURN_STARTED,
}

MATCHER = "^(permission_prompt|elicitation_dialog|elicitation_url_dialog|agent_needs_input|quota_auto_resume_stale)$"

WAITING = (
    "permission_prompt",
    "elicitation_dialog",
    "elicitation_url_dialog",
    "agent_needs_input",
    "quota_auto_resume_stale",
)

NOT_WAITING = (
    "idle_prompt",
    "auth_success",
    "elicitation_complete",
    "elicitation_response",
    "agent_completed",
    "quota_auto_resume_fired",
    "quota_auto_resume_disabled",
    "",
    "permission_prompt_x",
    "xidle_prompt",
)


def entry(label) -> dict:
    return {"hooks": [{"type": "command", "command": shlex.join((*HOOK, label))}]}


TUI_HOOKS = {
    "Stop": [entry("Stop")],
    "StopFailure": [entry("StopFailure")],
    "PermissionRequest": [entry("PermissionRequest")],
    "Notification": [{"matcher": MATCHER, **entry("Notification")}],
    "PostToolUse": [entry("PostToolUse")],
    "UserPromptSubmit": [entry("UserPromptSubmit")],
    "SessionStart": [{"matcher": "^(startup|resume)$", **entry("SessionStart")}],
}

TUI_SETTINGS = json.dumps({"hooks": TUI_HOOKS})

NAME = "w-1"

WANT_SETTINGS = "want a JSON object with a list of hooks per event"


def store() -> str:
    return os.path.join(os.path.realpath(os.path.expanduser("~")), ".ensemble/sessions/claude")


def reference(cwd: str, sid: str = SID) -> str:
    """ClaudeCommon.resume's text: no env value, only where its launch state is."""
    q = shlex.quote
    return f"cd {q(cwd)} && ensemble-session client-resume claude {q(store())} {q(cwd)} {sid}"


def tui_launch(
    work, prompt="hi", config=CONFIG, *, resume=False, permission=Permission.AUTO, **spec
) -> Launch:
    spec = Spec(
        **{
            "workdir": work,
            "access": Access(cwd=work),
            "sid": SID,
            "prompt": prompt,
            "resume": resume,
            **spec,
        }
    )
    run = ClaudeClient(config).tui()
    run._context = replace(LOCAL_CONTEXT, permission=permission)
    return run.launch(spec, HOOK)


def said(mid, *blocks) -> dict:
    """An assistant transcript record of message `mid` (None: no id)."""
    return {
        "type": "assistant",
        "message": {**({"id": mid} if mid else {}), "content": list(blocks)},
    }


LA = "America/Los_Angeles"


def epoch(stamp, zone=LA) -> float:
    return datetime.fromisoformat(stamp).replace(tzinfo=ZoneInfo(zone)).timestamp()


NOW = epoch("2026-10-11 10:00")
