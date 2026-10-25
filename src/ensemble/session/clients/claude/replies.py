"""Claude mid-turn replies. Claude Code queues a message sent while a turn runs (a `queue-operation`
enqueue record) and, once the running tool returns, joins it to that turn as a `queued_command`
attachment; no hook runs for it. A message one (commandMode prompt, not a background task's
notification) after the turn's first assistant record is an interjection;
the first assistant text after it is its answer, stored and announced as `reply <id>` by a detached
watcher (watch.py) or the turn's Stop hook, whichever finds it first. UserPromptSubmit opens the
turn, PostToolUse scans it and starts the watcher while a message waits, Stop scans with its text
and closes it. Only main-thread records count.
"""

import hashlib
import json
import os
import sys
import time

from ensemble.session.clients import replies as shared
from ensemble.session.clients.claude.common import read, size, texts
from ensemble.session.clients.replies import LIFETIME, Clock, Sleep, locked
from ensemble.session.engine.types import HookCall
from ensemble.utils import json_records, save

SCAN_MAX = 4 << 20  # bytes of the transcript one scan reads
STEERS_MAX = 16
DEADLINE = 5.0  # seconds from the hook's start
LABELS = ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "StopFailure")
WATCH = f"{__package__}.watch"  # run as python -P -s -m on this layer's source roots


def state_path(store: str, workdir: str, sid: str) -> str:
    key = hashlib.sha256(f"{workdir}\0{sid}".encode()).hexdigest()
    return os.path.join(store, "replies", f"{key}.json")


def _turn(*, open_: bool = False, since=0.0, start=0) -> dict:
    return {
        "open": open_,
        "since": since,
        "start": start,  # the transcript's size when the turn opened
        "cursor": start,  # bytes scanned
        "skip": False,  # the cursor is inside a line longer than SCAN_MAX
        "spoke": False,  # an assistant record since the turn opened or its last prompt
        "queued": 0,  # messages enqueued, not yet joined or taken out
        "steers": [],  # unanswered interjections' offsets
    }


def _fresh() -> dict:
    return {"version": 1, "name": "", "events": "", "transcript": "", "turn": _turn()}


def _offset(value) -> bool:
    return type(value) is int and value >= 0


def _valid(state) -> bool:
    turn = state.get("turn")
    return (
        set(state) == set(_fresh())
        and state["version"] == 1
        and all(isinstance(state[key], str) for key in ("name", "events", "transcript"))
        and isinstance(turn, dict)
        and set(turn) == set(_turn())
        and all(isinstance(turn[key], bool) for key in ("open", "skip", "spoke"))
        and isinstance(turn["since"], int | float)
        and all(_offset(turn[key]) for key in ("start", "cursor", "queued"))
        and isinstance(turn["steers"], list)
        and all(map(_offset, turn["steers"]))
    )


def _load(path: str) -> dict:
    return shared.load(path, _fresh, _valid)


def _record(found: dict, at: int, record: dict) -> None:
    """One main-thread transcript record's effect on the turn's progress."""
    kind = record.get("type")
    if record.get("isSidechain") is True:
        return
    if kind == "queue-operation":
        step = 1 if record.get("operation") == "enqueue" else -1
        found["queued"] = max(found["queued"] + step, 0)
    elif kind == "attachment":
        attachment = record.get("attachment")
        if isinstance(attachment, dict) and attachment.get("type") == "queued_command":
            found["queued"] = max(found["queued"] - 1, 0)
            sent = attachment.get("commandMode", "prompt") == "prompt"  # not a task's notice
            if found["spoke"] and sent:
                found["steers"] = [*found["steers"], at][-STEERS_MAX:]
    elif kind in ("user", "assistant"):
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        said = "\n".join(texts(content))
        if kind == "user":
            if isinstance(content, str) or said:  # a prompt: as ClaudeTui's, a new turn
                found.update(spoke=False, steers=[])
            return
        found["spoke"] = True
        if found["steers"] and said.strip() and record.get("isApiErrorMessage") is not True:
            found["answers"] += [(at, said) for at in found["steers"]]
            found["steers"] = []


def scan(state: dict) -> dict:
    """The turn's progress through the transcript's whole lines from its cursor, SCAN_MAX bytes:
    its fields, and `answers`, [(interjection offset, text)].
    """
    turn = state["turn"]
    found = {key: turn[key] for key in ("cursor", "skip", "spoke", "queued", "steers")}
    found["answers"] = []
    data = read(state["transcript"], offset=turn["cursor"], limit=SCAN_MAX) or b""
    complete = data[: data.rfind(b"\n") + 1]
    if not complete:
        if len(data) == SCAN_MAX:  # one line longer than a read: skip past it
            found.update(cursor=turn["cursor"] + len(data), skip=True)
        return found
    lines, position = complete.split(b"\n")[:-1], turn["cursor"]
    if found["skip"]:  # the rest of a line longer than a read
        position, lines = position + len(lines[0]) + 1, lines[1:]
        found["skip"] = False
    for line in lines:
        for record in json_records([line]):
            _record(found, position, record)
        position += len(line) + 1
    found["cursor"] = position
    return found


def _emit(state: dict, at: int, text: str) -> None:
    try:
        shared.emit(state["events"], state["name"], f"{state['transcript']}:{at}", text)
    except (OSError, ValueError) as error:
        print(f"claude reply: {error}", file=sys.stderr)


def _identity(turn: dict) -> tuple:
    return turn["since"], turn["start"]


def settle(
    path: str, *, deadline: float, clock: Clock, sleep: Sleep, final: str = "", close=False
) -> int:
    """Scans the open turn unlocked, then, locked again, applies the scan to the same turn unless
    another went further, emitting its answers (once each: by interjection), and, `close`, answers
    the rest with `final` and closes the turn; the answers and messages still awaited.
    """
    with locked(path, deadline, clock, sleep):
        state = _load(path)
    if not state["turn"]["open"]:
        return 0
    progress = scan(state)
    with locked(path, deadline, clock, sleep):
        current = _load(path)
        turn = current["turn"]
        if not turn["open"] or _identity(turn) != _identity(state["turn"]):
            return turn["queued"] + len(turn["steers"]) if turn["open"] else 0
        if progress["cursor"] >= turn["cursor"]:
            for at, text in progress.pop("answers"):
                _emit(current, at, text)
            turn.update(progress)
        if close:
            for at in turn["steers"] if final.strip() else ():
                _emit(current, at, final)
            current["turn"] = _turn(start=turn["cursor"])
        save(path, json.dumps(current))
        return current["turn"]["queued"] + len(current["turn"]["steers"])


def watch(path: str, *, clock: Clock | None = None, sleep: Sleep | None = None) -> int:
    """The watcher (shared.watch) of `path`'s open turn and its transcript."""
    clock, sleep = clock or time.monotonic, sleep or time.sleep
    return shared.watch(
        path,
        lambda deadline: settle(path, deadline=deadline, clock=clock, sleep=sleep),
        lambda: size(_load(path)["transcript"]),
        clock=clock,
        sleep=sleep,
        label="claude reply",
    )


def on_hook(call: HookCall, *, began: float) -> None:
    """The Claude client hook's reply bookkeeping, within DEADLINE s of `began` (the hook's
    time.monotonic() start); never raises, failures said on stderr.
    """
    try:
        _on_hook(call, began + DEADLINE)
    except Exception as error:  # noqa: BLE001  # replies must never fail the hook or keep its done line
        print(f"claude reply: {error}", file=sys.stderr)


def _on_hook(call: HookCall, deadline: float) -> None:
    name, store, events = call.state.get("name"), call.state.get("store"), call.events
    if call.label not in LABELS or not (isinstance(name, str) and name and events):
        return
    if not (isinstance(store, str) and os.path.isabs(store)):
        return
    clock, sleep, transcript = time.monotonic, time.sleep, str(call.state["transcript"])
    path = state_path(store, call.workdir, call.sid)
    os.makedirs(os.path.dirname(path), 0o700, exist_ok=True)
    shared.prune(os.path.dirname(path))
    if call.label in ("PostToolUse", "Stop", "StopFailure"):
        final = call.payload.get("last_assistant_message") if call.label == "Stop" else ""
        close = call.label != "PostToolUse"
        final = final if isinstance(final, str) else ""
        args = {"deadline": deadline, "clock": clock, "sleep": sleep}
        if settle(path, **args, final=final, close=close) and not close:
            shared.ensure_watcher(path, WATCH)
        return
    with locked(path, deadline, clock, sleep):
        current = _load(path)
        current.update(name=name, events=events, transcript=transcript)
        turn, now = current["turn"], time.time()
        kept = turn["open"] and now - turn["since"] <= LIFETIME
        if call.label == "SessionStart":
            current["turn"] = _turn()
        elif not kept:
            start = size(transcript)
            current["turn"] = _turn(open_=True, since=now, start=start)
        save(path, json.dumps(current))
    if call.label == "UserPromptSubmit" and kept:
        shared.ensure_watcher(path, WATCH)
