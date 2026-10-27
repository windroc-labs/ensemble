"""OpenCode's headless Mode: `opencode run --format json` behind launcher.py, which feeds it the
prompt on stdin (`run` quotes an argument holding a space). The first sessionID binds the run; text
parts are Replies, tools Activity, an error the failed turn; the end is the turn's, its last text.
opencode prints that sessionID only once its model streams, so a new run titles its session
TITLE: a resume of a run whose harness died before then finds it by that title (common.py).
"""

import os
import sys

from ensemble.session.clients.opencode import launcher
from ensemble.session.clients.opencode.common import (
    HEADLESS_RESERVED,
    NATIVE,
    TITLE,
    UNSET,
    Homed,
    OpenCodeCommon,
    image_path,
    refuse,
)
from ensemble.session.engine.types import (
    Activity,
    Blocked,
    ClientBound,
    Launch,
    Reply,
    Spec,
    Stderr,
    TurnEnd,
    TurnFailed,
    TurnStarted,
)
from ensemble.session.modes.client_adapters.headless import JsonHeadless

SESSION_TIMEOUT = 120  # seconds a run may take to print its first sessionID


class OpenCodeHeadless(Homed, JsonHeadless):
    _invalid_output = "opencode: invalid JSON event"

    def __init__(self, common: OpenCodeCommon):
        self.common = common
        self.cwd, self.sid = "", ""
        self.bound, self.failed, self.last = False, False, ""
        self.native, self.finished = "", False

    def launch(self, spec: Spec):
        context, common = self._context, self.common
        selection = common.selection(spec, context, HEADLESS_RESERVED)
        refuse(common.flags, HEADLESS_RESERVED, "[clients.opencode] flags")
        refuse(
            common.modes[context.permission],
            HEADLESS_RESERVED,
            f"[clients.opencode.modes] {context.permission}",
        )
        found = context.container
        cwd, sid = spec.access.cwd, spec.sid or ""
        model, variant = common.model(spec.tier), common.variant(spec.effort)
        env = common.environment(spec, context)
        command = (
            common.executable_for(env, cwd, context),
            "run",
            "--format",
            "json",
            *(selection or ("--title", TITLE.format(sid=sid))),
            "--model",
            model,
            *(("--variant", variant) if variant is not None else ()),
            *common.flags,
            *common.modes[context.permission],
            *spec.flags,
        )
        self.cwd, self.sid = cwd, sid
        self.bound, self.failed, self.last = False, False, ""
        self.native, self.finished = "", False
        return Launch(
            (
                found.python if found else os.path.realpath(sys.executable),
                "-I",
                image_path(found, launcher.__file__) if found else launcher.__file__,
                "--timeout",
                str(SESSION_TIMEOUT),
                "--prompt",
                spec.prompt,
                "--",
                *command,
            ),
            env=env,
            unset=UNSET,
            cwd=cwd,
            transcript=common.transcript(env, context),
            project=spec.access.project,
        )

    def events(self, lines):
        yield from super().events(lines)
        if not self.bound:
            yield TurnFailed("no_session", "opencode ended before it started a session")
        elif not self.failed and not self.finished:
            yield TurnFailed("incomplete_turn", "opencode ended without a final step")

    def parse(self, record: dict):
        native = record.get("sessionID")
        if self.bound and native != self.native:
            return
        if not self.bound and isinstance(native := record.get("sessionID"), str):
            if NATIVE.fullmatch(native):
                self.common.bind(self.cwd, self.sid, native)
                self.bound = True
                self.native = native
                yield ClientBound(native)
                yield TurnStarted()
            else:
                yield Stderr(f"opencode: invalid sessionID {native!r:.80}")
        kind, part = record.get("type"), record.get("part")
        part = part if isinstance(part, dict) else {}
        if not self.bound:
            if isinstance(kind, str):
                yield Activity(kind)
            return
        if kind == "text":
            value = part.get("text")
            if isinstance(value, str) and value.strip():
                self.last = value
                yield Reply(value)
            elif not isinstance(value, str):
                yield Activity(kind)
        elif kind == "tool_use":
            tool = part.get("tool")
            yield Activity(tool if isinstance(tool, str) else kind)
            state = part.get("state")
            if isinstance(state, dict) and state.get("status") == "error":
                error = state.get("error")
                yield Stderr(f"opencode: {tool}: {error}")
                if (
                    isinstance(error, str)
                    and "permission" in error.lower()
                    and any(word in error.lower() for word in ("rejected", "denied", "required"))
                ):
                    self.failed = True
                    yield TurnFailed("permission_required", error)
        elif kind in ("permission.asked", "question.asked"):
            self.failed = True
            yield Blocked(kind)
        elif kind == "step_finish" and part.get("reason") == "stop":
            if not self.failed and not self.finished:
                self.finished = True
                yield TurnEnd(self.last)
        elif kind == "error":
            self.failed = True
            error = record.get("error")
            error = error if isinstance(error, dict) else {}
            data = error.get("data")
            message = data.get("message") if isinstance(data, dict) else None
            name = error.get("name")
            yield TurnFailed(
                name if isinstance(name, str) else "error",
                message if isinstance(message, str) else "",
            )
        elif isinstance(kind, str):
            yield Activity(kind)
