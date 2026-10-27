"""Claude Code interactive launches, merged hooks, transcript replies and native usage limits.
Each new main-thread assistant text in the transcript since launch is one Reply, read from a cursor;
a turn end's text that lands later is not repeated. A usage limit comes from StopFailure or the
transcript's API error, once a turn, never from the screen.
"""

import json
import os
import re
import shlex
import time
from collections.abc import Sequence
from datetime import datetime, timedelta
from types import MappingProxyType
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ensemble.session.clients.claude import replies
from ensemble.session.clients.claude.common import (
    TAIL,
    TUI_ENV,
    ClaudeCommon,
    read,
    size,
    texts,
)
from ensemble.session.clients.common import UUID
from ensemble.session.engine.types import (
    ClientBound,
    ConfigError,
    HookCall,
    HookCallback,
    HookKind,
    LimitHit,
    Reply,
    SessionEvent,
    Spec,
    TurnEnd,
    TurnFailed,
    TurnStarted,
)
from ensemble.session.modes.client_adapters.tui import Tui
from ensemble.utils import number

LIMIT = re.compile(r"You've hit your ((?:[\w']+ ){0,3})limit|Usage limit reached")
WHEN = re.compile(
    (
        r"(?:([A-Za-z]+)(?: (\d{1,2})(?:, (\d{4}))?)?,? )?(\d{1,2})(?::([0-5]\d))?([ap]m)(?: \(([^)]*)\))?"
    ),
    re.I,
)
TRUST = re.compile(r"^\W*Quick safety check\b", re.M)  # the dialog's title starts its line
ZONE = re.compile(r"[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+){0,2}")
MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


class ClaudeTui(Tui):
    hooks = MappingProxyType(
        {
            "Stop": HookKind.TURN_END,
            "StopFailure": HookKind.TURN_FAILED,
            "PermissionRequest": HookKind.BLOCKED,
            "Notification": HookKind.BLOCKED,
            "PostToolUse": HookKind.ACTIVITY,
            "UserPromptSubmit": HookKind.TURN_STARTED,
        }
    )
    MATCHER = (
        "^(permission_prompt|elicitation_dialog|elicitation_url_dialog|agent_needs_input"
        "|quota_auto_resume_stale)$"
    )
    START, START_MATCHER = "SessionStart", "^(startup|resume)$"
    dialogs = ("Quick safety check",)
    opening = (START, "/color")
    _remote_owned = True

    def __init__(self, common: ClaudeCommon):
        super().__init__()
        self.common = common
        self.sid = self.name = ""  # name: its session's, for its reply lines
        self.spec: Spec | None = None
        self.transcript, self.start, self.offset = "", 0, 0
        self.last = self.unseen = ""  # the turn's newest text; its turn end's text not yet read
        self.limited = False  # this turn's LimitHit given
        self._limit_seen = False  # a transcript limit since the last turn-ending hook
        self._origin = self._config_dir = self._native = ""
        self._ids: set[str] = set()
        self._visited: set[str] = set()
        self._cutoff: str | None = None
        self._pending: tuple[str, str | None, str | None] | None = None

    def _home(self):
        return self.common.home()

    def launch(self, spec: Spec, hook):
        common = self.common
        self.sid, self.spec, self.name = spec.sid or "", spec, spec.name or ""
        matchers = {"Notification": self.MATCHER, self.START: self.START_MATCHER}
        hooks = {
            label: [
                {
                    **({"matcher": matchers[label]} if label in matchers else {}),
                    "hooks": [{"type": "command", "command": shlex.join((*hook, label))}],
                }
            ]
            for label in (*self.hooks, self.START)
        }
        tail = ("--", spec.prompt) if spec.prompt else ()
        launch = common.launch(
            spec, hooks, tail=tail, env=TUI_ENV, permission=self._context.permission
        )
        self._tracking(launch.transcript)
        self.start = self.offset = size(launch.transcript)
        return launch

    def commit(self):
        if self.spec is not None:
            self.common.save(self.spec)

    def hook_callback(self):
        state = {"transcript": self._origin, "start": self.start}
        return HookCallback(__name__, {**state, "store": self.common.store, "name": self.name})

    def restore(self, spec, state):
        self.sid, self.name = spec.sid or "", spec.name or ""
        path, start = state.get("transcript"), state.get("start")
        if not (
            isinstance(path, str) and os.path.isabs(path) and type(start) is int and start >= 0
        ):
            raise ConfigError("claude tui state: bad transcript cursor")
        self._tracking(path)
        self.start = self.offset = start

    def _tracking(self, path: str) -> None:
        self.transcript = self._origin = path
        self._config_dir = os.path.dirname(os.path.dirname(os.path.dirname(path)))
        self._native = os.path.splitext(os.path.basename(path))[0]
        if not UUID.fullmatch(self._native):
            self._native = self.sid
        chain = _chain(path, self.sid, self._config_dir)
        self._ids = {hop[0] for hop in chain}
        at = next((i for i, hop in enumerate(chain) if hop[0] == self._native), 0)
        self._visited = {hop[0] for hop in chain[: at + 1]}
        self._cutoff = None
        self._pending = chain[at + 1] if at + 1 < len(chain) else None

    def observe(self):
        found: list[Reply | LimitHit] = []
        while True:
            self.offset = min(self.offset, size(self.transcript))
            data = read(self.transcript, offset=self.offset) or b""
            end = data.rfind(b"\n") + 1
            self.offset += end
            for line in data[:end].split(b"\n"):
                try:
                    record = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if not isinstance(record, dict):
                    continue
                continuation = self.common.continuation(record, self._native, self._config_dir)
                if continuation:
                    self._ids.add(continuation[0])
                    self._pending = continuation
                timestamp = record.get("timestamp")
                if (
                    record.get("type") in ("user", "assistant")
                    and self._cutoff is not None
                    and isinstance(timestamp, str)
                    and timestamp <= self._cutoff
                ):
                    continue
                found.extend(self._record(record))
            if self._pending is None or self._pending[0] in self._visited:
                return found
            native, _, cutoff = self._pending
            target = self.common.continuation(
                {
                    "type": "continued-in",
                    "sessionId": self._native,
                    "continuedInSessionId": native,
                    "timestamp": cutoff,
                },
                self._native,
                self._config_dir,
            )
            if target is None or target[1] is None:
                return found
            self._native, path, self._cutoff = target
            assert path is not None
            self.transcript = path
            self._visited.add(native)
            self._pending = None
            self.offset = 0

    def _accept_turn(self, identifier):
        """Claude's hooks carry no turn id: a prompt while its turn is open is input Claude joins
        to that turn (a send, a background task's notice), unless a limit ended the turn unhooked.
        """
        if self._turn_open:
            if not self._limit_seen:
                return False
            self._limit_seen = False
        return super()._accept_turn(identifier)

    def _settle_turn(self, identifier, *, native=False):
        self._limit_seen = False
        return super()._settle_turn(identifier, native=native)

    def hook_events(self, label, payload):
        native = payload.get("session_id")
        if not isinstance(native, str) or native not in self._ids:
            return []
        return super().hook_events(label, payload)

    def replay(self, records):
        """Ends in the latest turn's state, not the oldest's: no LimitHit given live yet (the replay
        gives none), a Stop since the hook log's last prompt leaves its text unseen unless it is
        the transcript's newest, and only the newest turn's transcript limit ends its open turn.
        """
        found: list[SessionEvent | ClientBound] = [*self.observe()]
        newest, ended, limit = self.last, None, self.limited
        for label, payload in records:
            native = payload.get("session_id")
            if not isinstance(native, str) or native not in self._ids:
                continue
            events = self.hook_events(label, payload)
            found += events
            if label == "UserPromptSubmit":
                ended = None
            ended = next((e.reply for e in events if isinstance(e, TurnEnd)), ended)
        self._limit_seen = self._turn_open is True and limit  # the hooks cleared an older one
        self.limited = False
        self.last = newest if ended is None else ""
        self.unseen = ended if ended and ended.strip() and ended != newest else ""
        return [event for event in found if not isinstance(event, TurnStarted)]

    def _record(self, record: dict) -> Sequence[Reply | LimitHit]:
        if record.get("isSidechain") is True:
            return []
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if record.get("type") == "user":
            if isinstance(content, str) or texts(content):  # a prompt: a new turn
                self.last = self.unseen = ""
                self.limited = False
            return []
        if record.get("type") != "assistant":
            return []
        if record.get("isApiErrorMessage") is True:
            if record.get("error") != "rate_limit":
                return []
            quota = record.get("quotaLimits")
            quota = quota if isinstance(quota, dict) else {}
            resets = number(quota.get("resetsAt")) if quota.get("status") == "rejected" else None
            if resets is None:
                resets = _resets_in("\n".join(texts(content)))
            self._limit_seen = True  # read before or after its turn's hooks
            return self._limit(resets)
        text = "\n".join(texts(content))
        if not text.strip():
            return []
        if self.unseen and text.strip() == self.unseen.strip():
            self.unseen = ""
            return []
        self.last = text
        return [Reply(text)]

    def session_id(self, payload):
        """Keep ensemble identity stable across Claude native continuations."""
        return self.sid if super().session_id(payload) else ""

    def reply(self, payload):
        text = payload.get("last_assistant_message")
        if not isinstance(text, str):
            path = payload.get("transcript_path")
            try:
                data = read(path, limit=TAIL, tail=True) if isinstance(path, str) else None
            except ValueError:  # a NUL or a lone surrogate in the path
                data = None
            text = _last_text(data or b"")
        self.unseen = text if text.strip() and text != self.last else ""
        self.last = ""
        return text

    def pending(self, payload):
        tasks = payload.get("background_tasks")
        return len(tasks) if isinstance(tasks, list) else 0

    def activity(self, payload):
        return _first(payload, "tool_name")

    def blocked_reason(self, payload):
        return _first(payload, "tool_name", "message")

    def failure(self, payload):
        """StopFailure's typed error: rate_limit is the usage limit, any other a failed turn."""
        error = _first(payload, "error") or "unknown"
        if error == "rate_limit":
            return self._limit(_resets_in(_first(payload, "last_assistant_message")))
        return [TurnFailed(error, _first(payload, "error_details", "last_assistant_message"))]

    def shows(self, dialog, screen):
        if dialog != "Quick safety check":
            return super().shows(dialog, screen)
        return bool(TRUST.search(screen)) and super().shows("Yes, I trust this folder", screen)

    def _limit(self, resets: float | None) -> list[LimitHit]:
        if self.limited:
            return []
        self.limited = True
        return [LimitHit(resets)]


def on_hook(call: HookCall) -> dict | None:
    began = time.monotonic()
    path = call.state.get("transcript")
    if not isinstance(path, str) or not os.path.isabs(path):
        return None
    config_dir = os.path.dirname(os.path.dirname(os.path.dirname(path)))
    ids = {hop[0] for hop in _chain(path, call.sid, config_dir)}
    native = call.payload.get("session_id")
    if not isinstance(native, str) or native not in ids:
        return None
    replies.on_hook(call, began=began)  # a Stop's replies precede the Host hook's done
    return call.payload


def _chain(path: str, sid: str, config_dir: str) -> list[tuple[str, str | None, str | None]]:
    original = os.path.join(os.path.dirname(path), f"{sid}.jsonl")
    if not os.path.isfile(original):
        found = ClaudeCommon.continuation(
            {"type": "continued-in", "sessionId": sid, "continuedInSessionId": sid},
            sid,
            config_dir,
        )
        original = found[1] if found and found[1] else original
    return ClaudeCommon.walk(original, sid, config_dir)


def _first(payload: dict, *keys: str) -> str:
    values = (payload.get(key) for key in keys)
    return next((value for value in values if isinstance(value, str) and value), "")


def _last_text(data: bytes) -> str:
    """The text blocks of the last assistant message with text, over its records sharing one
    message.id.
    """
    last, found = None, []
    for line in reversed(data.split(b"\n")):
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            continue
        message = (
            record.get("message")
            if isinstance(record, dict) and record.get("type") == "assistant"
            else None
        )
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(message, dict) or not isinstance(content, list):
            continue
        blocks = texts(content)
        mid = message.get("id")
        mid = mid if isinstance(mid, str) and mid else object()
        if last is None:
            if blocks:
                last, found = mid, blocks
        elif mid != last:
            break
        else:
            found = blocks + found
    return "\n".join(found)


def _resets_in(message: str) -> float | None:
    """When the last usage-limit notice in `message` (no fast-mode one) says it resets."""
    message = " ".join(message.split())
    found = [m for m in LIMIT.finditer(message) if m[1] != "fast "]
    at = message.find("resets ", found[-1].end()) if found else -1
    when = WHEN.match(message, at + len("resets ")) if at >= 0 else None
    return None if when is None else _resets(when)


def _index(word: str, names: tuple[str, ...]) -> int | None:
    word = word.lower()
    return next((i for i, name in enumerate(names) if word in (name, name[:3])), None)


def _zone(name: str | None) -> ZoneInfo | None:
    if name and ZONE.fullmatch(name):
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            pass
    return None


def _resets(when: re.Match) -> float | None:
    word, day, year, hour, minute, half, name = when.groups()
    if not 1 <= int(hour) <= 12:
        return None
    hour, minute = int(hour) % 12 + (12 if half.lower() == "pm" else 0), int(minute or 0)
    zone = _zone(name)
    now = datetime.fromtimestamp(time.time(), zone)
    try:
        if day is not None:
            month = _index(word, MONTHS)
            if month is None:
                return None
            at = datetime(int(year or now.year), month + 1, int(day), hour, minute, tzinfo=zone)
            if year is None and at < now - timedelta(days=1):
                at = at.replace(year=now.year + 1)
            return at.timestamp()
        weekday = None if word is None else _index(word, WEEKDAYS)
        if word is not None and weekday is None:
            return None
        for ahead in range(8):
            on = now.date() + timedelta(days=ahead)
            at = datetime(on.year, on.month, on.day, hour, minute, tzinfo=zone)
            if at > now and weekday in (None, at.weekday()):
                return at.timestamp()
    except (ValueError, OverflowError, OSError):
        return None
