"""Codex steer replies: a prompt during an open turn is a steer; the next assistant message after
its user record in the session file is its answer, announced as `reply <id>` by the pump or the
turn's Stop or Interrupt hook. State lives under the store's replies/, locked; scans start at the
turn's start.
"""

import json
import os
import sys
import time
from typing import Any

from ensemble.session.clients import replies as shared
from ensemble.session.clients.codex.common import (
    RecoveryError,
    SessionStore,
    read_session,
    session_file,
    session_size,
)
from ensemble.session.clients.codex.tui import records
from ensemble.session.clients.replies import LIFETIME, Clock, Sleep, locked
from ensemble.session.engine import client_state
from ensemble.session.engine.types import HomePath, HomeStore, HookCall
from ensemble.utils import json_records, save

PENDING_MAX = 16
CLAIMED_MAX = 64
PROMPT_MAX = 4096  # characters of a normalized prompt kept for matching
SCAN_MAX = 1 << 20  # bytes of the session file one steer's scan reads
HOOK_SCAN_MAX = 4 << 20  # bytes of the session file one scan reads for all steers
DEADLINES = {"Interrupt": 1.5}  # seconds from the hook's start; its budget is 3 s, bind's 1 s
DEADLINE = 5.0  # other hooks', within their 10 s
LABELS = ("SessionStart", "UserPromptSubmit", "Stop", "Interrupt")


def state_path(store: str, workdir: str, sid: str) -> str:
    recovery = SessionStore(store).file(workdir, sid)
    return os.path.join(os.path.dirname(recovery), "replies", os.path.basename(recovery))


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _normalized(text: str) -> str:
    return " ".join(text.split())[:PROMPT_MAX]


def _turn(*, open_: bool = False, turn_id=None, since=0.0, start=0, prompt="") -> dict:
    return {
        "open": open_,
        "turn_id": turn_id,
        "since": since,
        "start": start,  # the session file's size at the turn's start
        "prompt": prompt,
        "opening": None,
        "claimed": [],  # steers' user record offsets
        "count": 0,
    }


def _fresh() -> dict:
    return {
        "version": 2,
        "name": "",
        "events": "",
        "store": "",
        "workdir": "",
        "sid": "",
        "native": "",
        "rollout": "",
        "turn": _turn(),
        "steers": [],
    }


def _offset(value) -> bool:
    return type(value) is int and value >= 0


def _steer_ok(steer) -> bool:
    return (
        isinstance(steer, dict)
        and set(steer) == {"n", "prompt", "cursor", "at", "skip"}
        and isinstance(steer["prompt"], str)
        and _offset(steer["n"])
        and _offset(steer["cursor"])
        and (steer["at"] is None or _offset(steer["at"]))
        and isinstance(steer["skip"], bool)
    )


def _turn_ok(turn) -> bool:
    return (
        isinstance(turn, dict)
        and set(turn) == set(_turn())
        and isinstance(turn["open"], bool)
        and (turn["turn_id"] is None or isinstance(turn["turn_id"], str))
        and isinstance(turn["since"], int | float)
        and _offset(turn["start"])
        and isinstance(turn["prompt"], str)
        and (turn["opening"] is None or _offset(turn["opening"]))
        and isinstance(turn["claimed"], list)
        and all(map(_offset, turn["claimed"]))
        and _offset(turn["count"])
    )


def _valid(state) -> bool:
    fresh = _fresh()
    if not isinstance(state, dict) or set(state) != set(fresh) or state["version"] != 2:
        return False
    texts = [key for key in fresh if key not in ("version", "turn", "steers")]
    return (
        all(isinstance(state[key], str) for key in texts)
        and _turn_ok(state["turn"])
        and isinstance(state["steers"], list)
        and all(map(_steer_ok, state["steers"]))
    )


def _load(path: str) -> dict[str, Any]:
    return shared.load(path, _fresh, _valid)


def _rollout(path: str) -> HomePath | None:
    """A session file's home path: the file is sessions/YYYY/MM/DD/<name> below its Codex home."""
    root = path
    for _ in range(5):
        root = os.path.dirname(root)
    try:
        return client_state.home_path(root, path)
    except OSError:
        return None


def _resolve(state: dict[str, Any], home: HomeStore) -> None:
    if state["rollout"]:
        return
    try:
        recovery = SessionStore(state["store"]).load(state["workdir"], state["sid"])
        if recovery is None or recovery.native_id is None:
            return
        found = session_file(home, recovery.snapshot, recovery.native_id)
    except (RecoveryError, OSError, ValueError):
        return
    if found is not None:
        state["native"], state["rollout"] = recovery.native_id, found.path


def _steer(turn: dict, turn_id: str | None, now: float) -> bool:
    """Codex's steer rule: the open turn, fresh, and the same turn id unless either has none."""
    if not turn["open"] or now - turn["since"] > LIFETIME:
        return False
    return turn["turn_id"] is None or turn_id is None or turn["turn_id"] == turn_id


def _matches(prompt: str, text: str) -> bool:
    found = _normalized(text)
    return not prompt or found == prompt


def scan(state: dict[str, Any], home: HomeStore, *, final: str = "", budget: int) -> dict[str, Any]:
    """Each pending steer's progress through the session file, `budget` bytes in all."""
    turn, rollout = state["turn"], _rollout(state["rollout"])
    opening, claimed, steers = turn["opening"], set(turn["claimed"]), {}
    for steer in state["steers"]:
        found = steers[steer["n"]] = {**steer, "answer": None}
        start = max(turn["start"], steer["cursor"])
        limit = min(SCAN_MAX, budget)
        data = read_session(home, rollout, offset=start, limit=limit) if limit and rollout else None
        if not data:
            continue
        budget -= len(data)
        complete = data[: data.rfind(b"\n") + 1]
        if not complete:
            if len(data) == SCAN_MAX:  # one line longer than a read: skip past it
                found.update(cursor=start + len(data), skip=True)
            continue
        position = start
        for line in complete.split(b"\n")[:-1]:
            here, position = position, position + len(line) + 1
            if found["skip"]:
                found["skip"] = False
                continue
            said = next((records.message(r) for r in json_records([line])), None)
            if said is None:
                continue
            role, text = said
            if role == "user":
                if opening is None and _matches(turn["prompt"], text):
                    opening = here
                elif (
                    found["at"] is None
                    and here != opening
                    and here not in claimed
                    and _matches(steer["prompt"], text)
                ):
                    found["at"] = here
                    claimed.add(here)
            elif found["at"] is not None and text.strip():
                found["answer"] = text
                break
        found["cursor"] = position
    for found in steers.values():
        if found["answer"] is None and found["at"] is not None and final.strip():
            found["answer"] = final
    return {"opening": opening, "claimed": sorted(claimed), "steers": steers}


def _identity(turn: dict) -> tuple:
    return turn["turn_id"], turn["since"], turn["start"]


def _emit(state: dict, at: int, text: str) -> None:
    shared.emit(state["events"], state["name"], f"{state['native']}:{at}", text)


def _commit(current: dict, progress: dict, *, close: bool) -> None:
    """Applies a scan's progress: emits each answer, keeps the rest pending."""
    turn = current["turn"]
    if turn["opening"] is None:
        turn["opening"] = progress["opening"]
    turn["claimed"] = sorted(set(turn["claimed"]) | set(progress["claimed"]))[-CLAIMED_MAX:]
    kept = []
    for steer in current["steers"]:
        found = progress["steers"].get(steer["n"])
        if found is None:
            kept.append(steer)
            continue
        steer.update(cursor=found["cursor"], at=found["at"], skip=found["skip"])
        if found["answer"] is None:
            kept.append(steer)
            continue
        try:
            _emit(current, found["at"], found["answer"])
        except (OSError, ValueError) as error:
            print(f"codex reply: {error}", file=sys.stderr)
    current["steers"] = kept
    if close:
        turn["open"], current["steers"] = False, []


def settle(
    path: str,
    *,
    deadline: float,
    budget: int,
    clock: Clock,
    sleep: Sleep,
    home: HomeStore,
    final: str = "",
    close: bool = False,
    resolve: bool = True,
) -> int:
    """Scans unlocked, then merges into the same open turn; the pending steers left."""
    with locked(path, deadline, clock, sleep):
        state = _load(path)
    if not state["turn"]["open"]:
        return 0
    if resolve:
        _resolve(state, home)
    progress = scan(state, home, final=final, budget=budget) if state["rollout"] else None
    with locked(path, deadline, clock, sleep):
        current = _load(path)
        if current["turn"]["open"] and _identity(current["turn"]) == _identity(state["turn"]):
            if not current["rollout"]:
                current.update(native=state["native"], rollout=state["rollout"])
            _commit(
                current, progress or {"opening": None, "claimed": [], "steers": {}}, close=close
            )
            save(path, json.dumps(current))
        return len(current["steers"]) if current["turn"]["open"] else 0


def settle_pending(store: str, workdir: str, sid: str, home: HomeStore) -> None:
    path = state_path(store, workdir, sid)
    shared.pumped(
        path,
        lambda deadline: settle(
            path,
            deadline=deadline,
            budget=HOOK_SCAN_MAX,
            clock=time.monotonic,
            sleep=time.sleep,
            home=home,
        ),
        "codex reply",
    )


def on_hook(call: HookCall, *, began: float, home: HomeStore) -> None:
    """Steer bookkeeping by `began` + its deadline; never raises."""
    try:
        _on_hook(call, began + DEADLINES.get(call.label, DEADLINE), home)
    except Exception as error:  # noqa: BLE001  # steer replies must never fail the hook or keep its done line
        print(f"codex reply: {error}", file=sys.stderr)


def _on_hook(call: HookCall, deadline: float, home: HomeStore) -> None:
    name, events, store = call.state.get("name"), call.events, call.state.get("store")
    if call.label not in LABELS or not (isinstance(name, str) and name and events):
        return
    if not isinstance(store, str):
        return
    clock, sleep, payload = time.monotonic, time.sleep, call.payload
    path = state_path(store, call.workdir, call.sid)
    shared.directory(os.path.dirname(path))
    shared.prune(os.path.dirname(path))
    if call.label in ("Stop", "Interrupt"):
        final = _text(payload.get("last_assistant_message")) if call.label == "Stop" else ""
        settle(
            path,
            deadline=deadline,
            budget=HOOK_SCAN_MAX,
            clock=clock,
            sleep=sleep,
            home=home,
            final=final,
            close=True,
            resolve=False,
        )
        return
    with locked(path, deadline, clock, sleep):
        state = _load(path)
    state.update(name=name, events=events, store=store, workdir=call.workdir, sid=call.sid)
    _resolve(state, home)  # walks the client's sessions tree: never under the lock
    turn_id, now = _text(payload.get("turn_id")) or None, time.time()
    prompt = _normalized(_text(payload.get("prompt")))
    with locked(path, deadline, clock, sleep):
        current = _load(path)
        current.update(name=name, events=events, store=store, workdir=call.workdir, sid=call.sid)
        if not current["rollout"]:
            current.update(native=state["native"], rollout=state["rollout"])
        turn = current["turn"]
        steered = call.label == "UserPromptSubmit" and _steer(turn, turn_id, now)
        if call.label == "SessionStart":
            current["turn"], current["steers"] = _turn(), []
        elif steered:
            steer = {"n": turn["count"], "prompt": prompt, "cursor": 0, "at": None, "skip": False}
            turn["count"] += 1
            current["steers"] = [*current["steers"], steer][-PENDING_MAX:]
        else:
            rollout = _rollout(current["rollout"]) if current["rollout"] else None
            start = session_size(home, rollout) if rollout else 0
            current["turn"] = _turn(
                open_=True, turn_id=turn_id, since=now, start=start, prompt=prompt
            )
            current["steers"] = []
        save(path, json.dumps(current))
