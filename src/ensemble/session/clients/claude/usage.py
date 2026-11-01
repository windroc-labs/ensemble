"""`claude -p /usage` text as Usage, ported from CodexBar's ClaudeStatusProbe.parse,
ClaudeUsageFetcher.makeSnapshot and ClaudeStatusProbe.parseResetDate (THIRD_PARTY_NOTICES.md).
A login without a subscription usage panel (an API key or gateway) prints only a cost summary,
read as Usage(None, None).
"""

import json
import math
import re
import time
from datetime import datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ensemble.session.engine.types import Usage, Window

INSIGHTS = "What's contributing to your limits usage?"
SESSION = "Current session"
WEEKLY = "Current week (all models)"
SESSION_SPAN = 5 * 3600
WEEK_SPAN = 7 * 24 * 3600
NO_PANEL = "no usage panel; claude may have sent /usage to the model as a prompt"
PERCENT = re.compile(r"([0-9]{1,3}(?:\.[0-9]+)?)[^\S\n]*%")
MODEL = re.compile(r"current\s*week\s*\(([^)]+)\)", re.I)
RESETS = (re.compile(r"(?i)\bresets?\b"), re.compile(r"\b(?:Reset|Resets)(?=[A-Z0-9])"))
WHEN = re.compile(
    r"(?:(?P<month>[a-z]{3}) (?P<day>\d{1,2})(?:,? (?P<year>\d{4}))?,? )?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))? ?(?P<half>am|pm)?",
    re.I,
)


COST_SUMMARY = re.compile(
    r"(?i)\s*(?:total cost:\s*\$?[0-9]|usage:\s*[0-9][0-9,]*\s+input,\s*[0-9][0-9,]*\s+output\b)"
)
COST_LINE = re.compile(
    r"(?i)\s*(?:"
    r"total cost:\s*\$?[0-9][0-9.,]*"
    r"|total duration \((?:api|wall)\):.*"
    r"|total code changes:.*"
    r"|usage:\s*[0-9][0-9,]*\s+input,.*"
    r"|usage by model:\s*"
    r")\s*"
    r"|\s+[\w.\-\[\]]+:\s+[0-9][0-9,]*\s+input,.*"
)


def _cost_only(text: str) -> bool:
    """Every line a cost-summary line, one of them the total cost or token usage."""
    lines = [line for line in text.splitlines() if line.strip()]
    return any(COST_SUMMARY.match(line) for line in lines) and all(
        COST_LINE.fullmatch(line) for line in lines
    )


def _norm(text: str) -> str:
    return "".join(c for c in text.lower() if c.isalnum())


def _compact(text: str) -> str:
    return "".join(text.lower().split())


def _distance(a: str, b: str) -> int:
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        diagonal, row[0] = row[0], i
        for j, y in enumerate(b, 1):
            diagonal, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, diagonal + (x != y))
    return row[-1]


def _model(line: str) -> str | None:
    found = MODEL.search(line)
    return found[1].strip() if found else None


def _all_models(model: str) -> bool:
    """Tolerates a garbled capture ("all modls")."""
    return _distance(_norm(model), "allmodels") <= 2


def _matches(line: str, label: str, *, fuzzy: bool) -> bool:
    if (want := _model(label)) is None:
        return _norm(label) in _norm(line)
    if (got := _model(line)) is None:
        return False
    if fuzzy and _all_models(want):
        return _all_models(got)
    return _norm(got) == _norm(want)


def _crosses(line: str, label: str, *, fuzzy: bool) -> bool:
    if not _norm(line).startswith("current"):
        return False
    if (want := _model(label)) is None:
        return _norm(label) not in _norm(line)
    if (got := _model(line)) is None:
        return True
    if fuzzy and _all_models(want):
        return not _all_models(got)
    return _norm(got) != _norm(want)


def _left(line: str) -> int | None:
    """The percent left a line states; None when it says neither used nor left."""
    lower = line.lower()
    if "|" in line and any(m in lower for m in ("opus", "sonnet", "haiku", "default")):
        return None
    if (found := PERCENT.search(line)) is None:
        return None
    value = max(0.0, min(100.0, float(found[1])))
    if any(word in lower for word in ("used", "spent", "consumed")):
        return math.floor(100 - value + 0.5)
    if any(word in lower for word in ("left", "remaining", "available")):
        return math.floor(value + 0.5)
    return None


def _reset(line: str) -> str | None:
    if (found := RESETS[0].search(line) or RESETS[1].search(line)) is None:
        return None
    cleaned = line[found.start() :].strip().strip(" )")
    cleaned = re.sub(r"(?i)\b([A-Za-z]{3}\s+\d{1,2})\s+t\s+(\d)", r"\1 at \2", cleaned)
    return cleaned + ")" if cleaned.count("(") > cleaned.count(")") else cleaned


def _find(lines: list[str], label: str, pick, span: int):
    """The first `pick` value in the `span` lines from `label`'s line, before the next label;
    an exact label match wins over a fuzzy one.
    """
    for fuzzy in (False, True):
        for k, line in enumerate(lines):
            if not _matches(line, label, fuzzy=fuzzy):
                continue
            for candidate in lines[k : k + span]:
                if _crosses(candidate, label, fuzzy=fuzzy):
                    break
                if (value := pick(candidate)) is not None:
                    return value
    return None


def _scoped(lines: list[str]) -> list[tuple[int, str | None]]:
    """(percent left, reset) of each model-scoped weekly window, the last copy of a model winning."""
    found: dict[str, tuple[int, str | None]] = {}
    for k, line in enumerate(lines):
        if (model := _model(line)) is None or not _norm(model) or _all_models(model):
            continue
        left = reset = None
        for candidate in lines[k : k + 14]:
            if _crosses(candidate, line, fuzzy=False):
                break
            left = _left(candidate) if left is None else left
            reset = _reset(candidate) if reset is None else reset
        if left is not None:
            found[_norm(model)] = (left, reset)
    return list(found.values())


def _percents(text: str) -> list[int]:
    """Every stated percent left, in order: a fallback when the labels moved."""
    compact = _compact(text)
    if not ("currentsession" in compact or "currentweek" in compact):
        return []
    if not any(word in compact for word in ("used", "left", "remaining", "available")):
        return []
    return [left for line in text.splitlines() if (left := _left(line)) is not None]


def _error(text: str) -> str | None:
    """The failure `/usage` reports instead of a panel."""
    if found := re.search(r"Failed\s*to\s*load\s*usage\s*data:\s*(\{.*\})", text, re.S):
        try:
            error = json.loads(found[1].replace("\r", "").replace("\n", "")).get("error")
        except (ValueError, AttributeError, RecursionError):
            error = None
        if isinstance(error, dict):
            if str(error.get("type", "")).strip().lower() == "rate_limit_error":
                return "usage endpoint rate limited"
            details = error.get("details")
            code = details.get("error_code") if isinstance(details, dict) else None
            hint = " ".join(
                part
                for part in (str(error.get("message") or "").strip(), f"({code})" if code else "")
                if part
            )
            if hint:
                return hint
    lines = re.findall(r"(?i)failed\s*to\s*load\s*usage\s*data[^\r\n]*", text)
    failure = lines[-1].lower() if lines else ""
    if "token_expired" in failure or "token has expired" in failure:
        return "token expired"
    if "authentication_error" in failure:
        return "authentication error"
    if "rate_limit_error" in failure or "rate limited" in failure or "ratelimited" in failure:
        return "usage endpoint rate limited"
    compact = _compact(text)
    quota = ("currentsession", "currentweek", "%used", "%left", "%remaining", "%available")
    notice = "currentlyusingyoursubscription" in compact and "claudecodeusage" in compact
    if notice and not any(word in compact for word in quota):
        return "subscription notice without quota data"
    if "failedtoloadusagedata" in compact:
        return "could not load usage data"
    return None


def _zone(name: str) -> tzinfo | None:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _occurrences(zone, wall: tuple[int, int, int, int, int]) -> list[float]:
    """Each instant the wall time names in `zone` (None: local): none in a DST gap, two in a repeat."""
    found = []
    for fold in (0, 1):
        try:
            local = datetime(*wall, tzinfo=zone, fold=fold)
        except ValueError:
            return []
        stamp = local.timestamp()
        back = datetime.fromtimestamp(stamp, zone).replace(tzinfo=None, fold=0)
        if back == local.replace(tzinfo=None, fold=0) and stamp not in found:
            found.append(stamp)
    return found


def _resolve(candidates: list[float], now: float, span: float | None) -> float | None:
    """The next occurrence; a recent past one instead when the next cannot be this window's."""
    ordered = sorted(candidates)
    future = next((stamp for stamp in ordered if stamp >= now), None)
    if future is None:
        return ordered[-1] if ordered else None
    if span is None:
        return future
    past = next((stamp for stamp in reversed(ordered) if stamp < now), None)
    if past is not None and now - past <= span and future - now > span:
        return past
    return future


def reset_at(text: str | None, now: float, span: float | None = None) -> float | None:
    """A reset description's epoch seconds, in its parenthesized zone, else local time."""
    raw = re.sub(r"(?i)^resets?:?\s*", "", (text or "").strip())
    raw = re.sub(r"(?i) at ", " ", raw)
    raw = re.sub(r"(?i)\b([A-Za-z]{3})(\d)", r"\1 \2", raw)
    raw = re.sub(r",(\d)", r", \1", raw)
    raw = re.sub(r"(?i)(\d)at(?=\d)", r"\1 ", raw)
    raw = re.sub(r"(?<=\d)\.(\d{2})\b", r":\1", raw)
    zone = None
    if found := re.search(r"\(([^)]+)\)", raw):
        zone = _zone(found[1].strip())
        raw = raw[: found.start()] + raw[found.end() :]
    if not (found := WHEN.fullmatch(" ".join(raw.split()))):
        return None
    hour, minute = int(found["hour"]), int(found["minute"] or 0)
    if found["half"]:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if found["half"].lower() == "pm" else 0)
    elif found["minute"] is None and found["year"] is None:
        return None
    if hour > 23 or minute > 59:
        return None
    if found["month"] is None:
        today = datetime.fromtimestamp(now, zone).date()
        days = (today + timedelta(days=offset) for offset in (-1, 0, 1))
        candidates = [
            stamp
            for date in days
            for stamp in _occurrences(zone, (date.year, date.month, date.day, hour, minute))
        ]
    else:
        try:
            # %b is English: ensemble never sets LC_TIME.
            month = datetime.strptime(found["month"], "%b").month
        except ValueError:
            return None
        day = int(found["day"])
        this = datetime.fromtimestamp(now, zone).year
        years = [int(found["year"])] if found["year"] else range(this - 8, this + 9)
        candidates = [
            stamp
            for year in years
            for stamp in _occurrences(zone, (year, month, day, hour, minute))
        ]
    return _resolve(candidates, now, span)


def _window(left: int | None, reset: str | None, now: float, span: int) -> Window | None:
    if left is None:
        return None
    reset = reset.strip() if reset else None
    return Window(max(0.0, min(100.0, 100.0 - left)), reset_at(reset, now, span))


def parse(text: str, now: float | None = None) -> Usage:
    """The session window and the most-used weekly one (all models or one model's); no window
    for a cost summary alone; ValueError with the reason when `text` holds neither.
    """
    now = time.time() if now is None else now
    if found := re.search(re.escape(INSIGHTS), text, re.I):
        text = text[: found.end()]
    if not text.strip():
        raise ValueError("no output")
    if (error := _error(text)) is not None:
        raise ValueError(error)
    if "loadingusage" in _compact(text) and not _percents(text):
        raise ValueError("still loading")
    if _cost_only(text):
        return Usage(None, None)
    lines = text.splitlines()
    session = _find(lines, SESSION, _left, 12)
    if session is None:
        session = next(iter(_percents(text)), None)
    if session is None:
        raise ValueError(NO_PANEL)
    has_weekly = any(_all_models(model) for model in map(_model, lines) if model)
    weekly_reset = _find(lines, WEEKLY, _reset, 14) if has_weekly else None
    weeks = [
        _window(_find(lines, WEEKLY, _left, 12), weekly_reset, now, WEEK_SPAN),
        *(_window(left, reset or weekly_reset, now, WEEK_SPAN) for left, reset in _scoped(lines)),
    ]
    return Usage(
        _window(session, _find(lines, SESSION, _reset, 14), now, SESSION_SPAN),
        max(filter(None, weeks), key=lambda window: window.used_percent, default=None),
    )
