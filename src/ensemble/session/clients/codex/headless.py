"""Codex headless launches and native JSON events with session binding, usage limits and failed
turns.
"""

import os

from ensemble.session.clients.codex.common import (
    CodexCommon,
    launch_flags,
    limit_message,
    native_id,
    reset_at,
)
from ensemble.session.clients.codex.common.types import SessionState
from ensemble.session.clients.common import split_flags
from ensemble.session.engine.types import (
    Activity,
    Blocked,
    ClientBound,
    Launch,
    LimitHit,
    Reply,
    Spec,
    Stderr,
    TurnEnd,
    TurnFailed,
    TurnStarted,
)
from ensemble.session.modes.client_adapters.headless import JsonHeadless


class CodexHeadless(JsonHeadless):
    _invalid_output = "codex: invalid JSON event"

    def _home(self):
        return self.common.home()

    def __init__(self, common: CodexCommon):
        self.common = common
        self.state: SessionState | None = None
        self.last_reply = ""
        self.limit_sent = False
        self.started = False

    def launch(self, spec: Spec):
        resume = spec.resume
        self.state = self.common.prepare(spec)
        self.limit_sent, self.started, self.last_reply = False, False, ""
        snapshot = self.state.snapshot
        found = self._context.container
        executable = os.path.join(found.bin_dir, "codex") if found is not None else "codex"
        mode = self.common.modes[self._context.permission]
        argv = [
            executable,
            "exec",
            *launch_flags(snapshot, interactive=False, mode=mode, configured=self.common.flags),
        ]
        if resume:
            argv.extend(self.common.selector(self.state.native_id, resume=True))
        _, literal = split_flags(snapshot.flags)
        argv.extend((*literal, spec.prompt) if literal else ("--", spec.prompt))
        return Launch(
            tuple(argv),
            env=self.common.environment(snapshot),
            unset=self.common.UNSET,
            cwd=snapshot.cwd,
            project=snapshot.project,
            transcript=self.common.transcript(self.state) if resume else "",
            resume=self.common.resume_command(self.state) if resume else "",
        )

    def commit(self) -> None:
        self.state = self.common.commit(self.state)

    def parse(self, record):
        kind = record.get("type")
        if kind == "thread.started":
            found = record.get("thread_id")
            if not isinstance(found, str) or not native_id(found):
                yield Stderr("codex: invalid thread.started ID")
                return
            if self.state is None:
                yield Stderr("codex: thread.started before launch")
                return
            self.state = self.common.bind(self.state.workdir, self.state.sid, found)
            yield ClientBound(found)
        elif kind == "turn.started":
            if not self.started:
                self.started, self.last_reply = True, ""
                yield TurnStarted()
        elif kind in ("permission.requested", "approval.requested", "user_input.requested"):
            yield Blocked(kind)
        elif kind == "item.completed":
            item = record.get("item")
            if (
                isinstance(item, dict)
                and item.get("type") == "agent_message"
                and isinstance(item.get("text"), str)
            ):
                self.last_reply = item["text"]
                yield Reply(self.last_reply)
            else:
                yield Activity(kind)
        elif kind == "turn.completed":
            yield TurnEnd(self.last_reply)
        elif kind in ("error", "turn.failed"):
            yield from self._error(record, kind)
        elif isinstance(kind, str):
            yield Activity(kind)

    def _error(self, record, kind):
        error = record.get("message") if kind == "error" else record.get("error")
        info = error.get("codex_error_info") if isinstance(error, dict) else None
        if isinstance(info, dict) and len(info) == 1:
            info = next(iter(info))
        if isinstance(error, dict):
            error = error.get("message")
        found = error if isinstance(error, str) and error.strip() else ""
        if found:
            yield Stderr(found)
            if not self.limit_sent and limit_message(found):
                self.limit_sent = True
                yield LimitHit(reset_at(found, self.common.config.get("env", {})))
        if kind == "turn.failed" and not self.limit_sent:  # an `error` may precede a retry
            yield TurnFailed(info if isinstance(info, str) and info else "turn_failed", found)
