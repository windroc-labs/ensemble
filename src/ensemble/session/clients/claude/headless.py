"""Claude Code's headless Mode, `claude -p` with stream-json output. launch is ClaudeCommon's with
the prompt last after "--" so a leading "-" stays the prompt. parse turns init, assistant, result
and rate-limit records into events, one LimitHit per run.
"""

import os

from ensemble.session.clients.claude.common import ClaudeCommon
from ensemble.session.engine.types import (
    Activity,
    Blocked,
    ClientBound,
    LimitHit,
    Reply,
    Spec,
    TurnEnd,
    TurnFailed,
    TurnStarted,
)
from ensemble.session.modes.client_adapters.headless import JsonHeadless
from ensemble.utils import number


class ClaudeHeadless(JsonHeadless):
    _owned = True

    def __init__(self, common: ClaudeCommon):
        self.common = common
        self.limit_sent = False
        self.resets_at = None
        self.sid = ""
        self.spec: Spec | None = None
        self.started = False
        self._native = ""

    def launch(self, spec: Spec):
        common = self.common
        self.sid, self.spec = spec.sid or "", spec
        self.started, self.limit_sent, self.resets_at = False, False, None
        launched = common.launch(
            spec,
            head=("-p",),
            output=("--output-format", "stream-json", "--verbose"),
            tail=("--", spec.prompt),
            env=common.ENV,
            permission=self._context.permission,
        )
        self._native = os.path.basename(launched.transcript).removesuffix(".jsonl") or self.sid
        return launched

    def _home(self):
        return self.common.home()

    def commit(self):
        if self.spec is not None:
            self.common.save(self.spec)

    def parse(self, record):
        native = record.get("session_id")
        if native is not None and native != self._native:
            return
        kind = record.get("type")
        if kind == "system":
            native = record.get("session_id")
            if record.get("subtype") == "init" and isinstance(native, str) and native and self.sid:
                yield ClientBound(self.sid)  # its native id is the sid it launched
                if not self.started:
                    self.started = True
                    yield TurnStarted()
        elif kind == "rate_limit_event":
            info = record.get("rate_limit_info")
            if isinstance(info, dict) and info.get("status") == "rejected":
                if (resets := number(info.get("resetsAt"))) is not None:
                    self.resets_at = resets
                yield from self._limit()
        elif kind == "assistant":
            if record.get("error") == "rate_limit":
                yield from self._limit()
            yield from self._assistant(record)
        elif kind == "result":
            result = record.get("result")
            denied, subtype = record.get("permission_denials"), record.get("subtype")
            if isinstance(denied, list) and denied:
                yield TurnFailed("permission_required", "headless permission denied")
            elif record.get("is_error") is True or subtype != "success":
                errors = record.get("errors")
                detail = (
                    "\n".join(e for e in errors if isinstance(e, str))
                    if isinstance(errors, list)
                    else ""
                )
                yield TurnFailed(
                    subtype if isinstance(subtype, str) and subtype else "incomplete_result",
                    detail or (result if isinstance(result, str) else ""),
                )
            elif isinstance(result, str):
                yield TurnEnd(result, 0)
            else:
                yield TurnFailed("incomplete_result", "success result has no final reply")
        elif kind == "control_request":
            request = record.get("request")
            if isinstance(request, dict) and request.get("subtype") == "can_use_tool":
                tool = request.get("tool_name")
                yield Blocked(tool if isinstance(tool, str) else "permission")

    def _limit(self):
        if not self.limit_sent:
            self.limit_sent = True
            yield LimitHit(self.resets_at)

    def _assistant(self, record):
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
        text = "\n".join(
            b["text"] for b in blocks if b.get("type") == "text" and isinstance(b.get("text"), str)
        )
        if text.strip():
            yield Reply(text)
        for block in blocks:
            if block.get("type") == "tool_use":
                name = block.get("name")
                yield Activity(name if isinstance(name, str) else "")
