"""OpenCode's TUI Mode: `opencode`, its first prompt by --prompt, a resume's by the opening once
plugin.js starts. plugin.js turns OpenCode's events into hook calls carrying its root session id;
on_hook binds a run to the first one and drops any other.
"""

import json
import os
from pathlib import Path
from types import MappingProxyType

from ensemble.session.clients.common import question
from ensemble.session.clients.opencode.common import (
    NATIVE,
    TUI_RESERVED,
    UNSET,
    Bindings,
    Homed,
    OpenCodeCommon,
    image_path,
    refuse,
)
from ensemble.session.engine.types import (
    ConfigError,
    HookCall,
    HookCallback,
    HookKind,
    Launch,
    Reply,
    Spec,
    TurnFailed,
)
from ensemble.session.modes.client_adapters.tui import Tui

PLUGIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugin.js")
START = "plugin.start"  # plugin.js's first call on a resume
HOOK_ENV, SESSION_ENV = "ENSEMBLE_OPENCODE_HOOK", "ENSEMBLE_OPENCODE_SESSION"
PURE = "OPENCODE_PURE"  # it would skip plugin.js


def _text(payload: dict, key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def _paths(state) -> tuple[str, str] | None:
    cwd, store = state.get("cwd"), state.get("store")
    if all(isinstance(p, str) and os.path.isabs(p) for p in (cwd, store)):
        return cwd, store
    return None


class OpenCodeTui(Homed, Tui):
    hooks = MappingProxyType(
        {
            "session.idle": HookKind.TURN_END,
            "turn.started": HookKind.TURN_STARTED,
            "session.error": HookKind.TURN_FAILED,
            "permission.asked": HookKind.BLOCKED,
            "question.asked": HookKind.BLOCKED,
            "session.busy": HookKind.ACTIVITY,
            "permission.replied": HookKind.ACTIVITY,
            "question.replied": HookKind.ACTIVITY,
            "question.rejected": HookKind.ACTIVITY,
            "tool": HookKind.ACTIVITY,
        }
    )

    def __init__(self, common: OpenCodeCommon):
        self.common = common
        self.cwd, self.sid, self.fresh = "", "", False
        self.opening = None

    def _configure_hooks(self):
        auto = "--auto" in self.common.modes[self._context.permission]
        # Both the commander's hook state and replay use this mapping.
        self.hooks = MappingProxyType(
            {
                label: kind
                for label, kind in type(self).hooks.items()
                if label != "permission.asked" or not auto
            }
        )

    def launch(self, spec: Spec, hook):
        context, common = self._context, self.common
        selection = common.selection(spec, context, TUI_RESERVED)
        mode = common.modes[context.permission]
        refuse(common.flags, TUI_RESERVED, "[clients.opencode] flags")
        refuse(mode, TUI_RESERVED, f"[clients.opencode.modes] {context.permission}")
        self._configure_hooks()
        for where, env in (
            ("[clients.opencode] env", common.env),
            ("a run's env", {**spec.access.env, **spec.env}),
        ):
            if PURE in env:
                raise ConfigError(f"{where}: {PURE} would skip the opencode client's plugin")
        model = common.model(spec.tier)
        self.cwd, self.sid = os.path.realpath(spec.access.cwd), spec.sid or ""
        self.fresh = not spec.resume
        prompt = (f"--prompt={spec.prompt}",) if spec.prompt and not spec.resume else ()
        self.opening = (START, spec.prompt) if spec.prompt and spec.resume else None
        found = context.container
        plugin = Path(image_path(found, PLUGIN)) if found else Path(PLUGIN).resolve()
        env = common.environment(spec, context, plugin=plugin.as_uri())
        env[HOOK_ENV] = json.dumps(list(hook))
        env[SESSION_ENV] = (common.native(self.cwd, self.sid) or "") if spec.resume else ""
        return Launch(
            (
                common.executable_for(env, spec.access.cwd, context),
                *selection,
                "--model",
                model,
                *common.flags,
                *mode,
                *spec.flags,
                *prompt,
            ),
            env=env,
            unset=(*UNSET, PURE),
            cwd=spec.access.cwd,
            transcript=common.transcript(env, context),
            project=spec.access.project,
        )

    def commit(self):
        """A fresh start's hooks bind its first session, so the run's old binding goes first."""
        if self.fresh:
            self.common.unbind(self.cwd, self.sid)

    def hook_callback(self):
        return HookCallback(__name__, {"cwd": self.cwd, "store": self.common.store})

    def restore(self, spec, state):
        if _paths(state) is None:
            raise ConfigError("opencode tui state: bad state")
        self._configure_hooks()
        self.sid = spec.sid or ""

    def reply(self, payload):
        return _text(payload, "last_assistant_message")

    def hook_events(self, label, payload):
        native = self.session_id(payload)
        if not native or self.client and native != self.client:
            return []
        if (
            label == "message.completed"
            and self._turn_id
            and payload.get("turn_id") != self._turn_id
        ):
            return []
        found = super().hook_events(label, payload)
        if label == "message.completed" and (text := _text(payload, "text")).strip():
            found.append(Reply(text))
        return found

    def failure(self, payload):
        return [TurnFailed(_text(payload, "error") or "unknown", _text(payload, "message"))]

    def blocked_reason(self, payload):
        if "questions" in payload:
            return question(payload["questions"])
        return _text(payload, "permission")

    def activity(self, payload):
        return _text(payload, "tool_name")


def on_hook(call: HookCall) -> dict | None:
    """Journals a call of the run's session, binding an unbound run to it; drops any other."""
    native, paths = call.payload.get("session_id"), _paths(call.state)
    if not isinstance(native, str) or not NATIVE.fullmatch(native) or paths is None:
        return None
    cwd, store = paths
    bindings = Bindings(store)
    try:
        bound = bindings.native(cwd, call.sid)
    except ConfigError:
        bound = None
    if bound is None:
        try:
            bindings.bind(cwd, call.sid, native)
        except ConfigError:
            return None
    elif bound != native:
        return None
    return call.payload
