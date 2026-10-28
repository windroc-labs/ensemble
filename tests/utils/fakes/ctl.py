"""The fake ctl, installed as ensemble-orchestrator and run as `python3 -P ctl.py <dir> <sub> …`:
status, show (exit 1: cannot show) and close answer the stdin Records (bad line: exit 2) from
<dir>/sessions.json (name → alive, state, outcome, showable, hidden, display; unknown: gone);
start NAME [--display D] and resume NAME print the Record, resuming a live one starts nothing;
content NAME or one stdin Record prints its content. Start honors ENSEMBLE_SID or makes a UUID;
resume keeps the saved sid, rejecting a mismatch. Calls go to <dir>/calls.jsonl.
"""

import argparse
import json
import os
import shlex
import sys
import uuid

from ensemble.utils import files
from ensemble.utils.record import Record, RecordError, Status

COMMAND = "ensemble-orchestrator"
SCRIPT = os.path.abspath(__file__)
PYTHONPATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(SCRIPT)))), "src"
)
SESSIONS = "sessions.json"
CALLS = "calls.jsonl"
GONE = {
    "alive": False,
    "state": "gone",
    "outcome": None,
    "showable": False,
    "hidden": False,
    "display": None,
    "sid": None,
}


def install(bin_dir: str, state_dir: str) -> str:
    os.makedirs(state_dir, exist_ok=True)
    path = os.path.join(bin_dir, COMMAND)
    argv = ("env", f"PYTHONPATH={PYTHONPATH}", sys.executable, "-P", "-S", SCRIPT)
    files.save(path, f'#!/bin/sh\nexec {shlex.join((*argv, os.path.abspath(state_dir)))} "$@"\n')
    os.chmod(path, 0o755)
    return COMMAND


def record(name: str, display: str | None = None, *, sid: str | None = None) -> Record:
    resume = (COMMAND, "resume", name)
    return Record(1, "fake", name, (name,), sid, None, (COMMAND,), resume, display, None)


def lines(*items: Record | Status) -> str:
    return "".join(i.to_line() + "\n" for i in items)


def set_session(state_dir: str, name: str, **fields) -> None:
    if unknown := fields.keys() - GONE.keys():
        raise TypeError(f"set_session: unknown fields {sorted(unknown)}")
    sessions = _sessions(state_dir)
    sessions[name] = {**_session(sessions, name), **fields}
    _save(state_dir, sessions)


def calls(state_dir: str) -> list[dict]:
    try:
        with open(os.path.join(state_dir, CALLS), encoding="utf-8") as f:
            return [json.loads(line) for line in f]
    except FileNotFoundError:
        return []


def _sessions(state_dir: str) -> dict:
    try:
        with open(os.path.join(state_dir, SESSIONS), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _session(sessions: dict, name: str) -> dict:
    return {**GONE, **sessions.get(name, {})}


def _save(state_dir: str, sessions: dict) -> None:
    files.save(os.path.join(state_dir, SESSIONS), json.dumps(sessions))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=COMMAND)
    subs = parser.add_subparsers(dest="sub", required=True)
    for sub in ("status", "show", "close"):
        subs.add_parser(sub)
    subs.add_parser("content").add_argument("name", nargs="?")
    start = subs.add_parser("start")
    start.add_argument("name")
    start.add_argument("--display")
    subs.add_parser("resume").add_argument("name")
    return parser


def _start(state_dir: str, args, call: dict) -> int:
    sessions = _sessions(state_dir)
    session = _session(sessions, args.name)
    display = args.display if args.sub == "start" else session["display"]
    supplied = os.environ.get("ENSEMBLE_SID")
    if args.sub == "resume":
        sid = session["sid"]
        if sid is None:
            print(f"{COMMAND}: {args.name}: no sid to resume", file=sys.stderr)
            return 2
        if supplied is not None and supplied != sid:
            print(f"{COMMAND}: {args.name}: ENSEMBLE_SID does not match saved sid", file=sys.stderr)
            return 2
    else:
        sid = str(uuid.uuid4()) if supplied is None else supplied
    try:
        started = record(args.name, display, sid=sid)
    except RecordError as e:
        print(f"{COMMAND}: {e}", file=sys.stderr)
        return 2
    call["names"] = [args.name]
    if args.sub == "start" or not session["alive"]:
        live = {"alive": True, "state": "working", "outcome": None, "display": display, "sid": sid}
        sessions[args.name] = {**session, **live}
        _save(state_dir, sessions)
    print(started.to_line())
    return 0


def _answer(state_dir: str, args, call: dict) -> int:
    sub = args.sub
    try:
        if sub == "content" and args.name is not None:
            records = [record(args.name)]
        else:
            records = [Record.parse(line) for line in sys.stdin]
        if sub == "content" and len(records) != 1:
            raise RecordError("content wants one key or Record")
    except RecordError as e:
        print(f"{COMMAND}: stdin: {e}", file=sys.stderr)
        return 2
    call["names"] = [each.name for each in records]
    sessions = _sessions(state_dir)
    failed = 0
    for each in records:
        session = _session(sessions, each.name)
        if sub == "content":
            print(f"content {each.name}")
        elif sub == "status":
            shown = session["alive"] and session["showable"]
            attach = f"{COMMAND} attach {each.name}" if shown else None
            status = Status(
                name=each.name,
                alive=session["alive"],
                state=session["state"],
                outcome=session["outcome"],
                attach=attach,
                hidden=session["hidden"],
            )
            print(status.to_line())
        elif sub == "show" and not session["showable"]:
            print(f"session {each.name}: host fake cannot show", file=sys.stderr)
            failed = 1
        elif sub == "close":
            sessions[each.name] = {**session, "alive": False, "state": "gone"}
    if sub == "close":
        _save(state_dir, sessions)
    return failed


def main(state_dir: str, *argv: str) -> int:
    call = {"argv": list(argv), "names": []}
    try:
        args = _parser().parse_args(argv)
        if args.sub in ("start", "resume"):
            return _start(state_dir, args, call)
        return _answer(state_dir, args, call)
    finally:
        files.append_line(os.path.join(state_dir, CALLS), json.dumps(call) + "\n")


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
