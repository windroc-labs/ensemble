"""A fake `opencode run --format json`: logs each call (argv, cwd, stdin prompt, its OpenCode
config and env) to FAKE_LOG and plays FAKE_SCENARIO's steps ("resume" for a --session run) as
JSON lines carrying its session id. A step: {"text"}, {"tool"}, {"bash": command} (refused, as
`run` refuses an "ask", unless --auto or an allow rule for it), {"error": {...}}, {"err": line},
{"sleep": seconds}, {"exit": code}. Scenario "session": another id; "unbound": lines without one.
`--version` prints FAKE_OPENCODE_VERSION (default 1.19.3).
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

NATIVE = "ses_0123456789abFakeFakeFakeFa"
REJECTED = "The user rejected permission to use this specific tool call."
ENV = (
    "OPENCODE_DISABLE_AUTOUPDATE",
    "OPENCODE_PERMISSION",
    "OPENCODE_PID",
    "PWD",
)


def parse(args):
    if not args or args[0] != "run":
        raise RuntimeError("expected run")
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--format", choices=("json",), required=True)
    parser.add_argument("--session")
    parser.add_argument("--model")
    parser.add_argument("--variant")
    parser.add_argument("--agent")
    parser.add_argument("--title")
    parser.add_argument("--auto", action="store_true")
    return parser.parse_args(args[1:])


def allowed(config: dict, command: str, *, auto: bool) -> bool:
    rule = config.get("permission", {}).get("bash", "ask")
    found = rule if isinstance(rule, str) else rule.get(command, rule.get("*", "ask"))
    return auto or found == "allow"


def main():
    args = sys.argv[1:]
    if args == ["--version"]:
        print(os.environ.get("FAKE_OPENCODE_VERSION", "1.19.3"))
        return 0
    options = parse(args)
    prompt = sys.stdin.read()
    config = json.loads(os.environ.get("OPENCODE_CONFIG_CONTENT", "{}"))
    scenario = json.loads(Path(os.environ["FAKE_SCENARIO"]).read_text())
    native = scenario.get("session", options.session or NATIVE)
    refused = []

    def emit(kind, **fields):
        bound = {} if scenario.get("unbound") else {"sessionID": native}
        print(json.dumps({"type": kind, "timestamp": 1, **bound, **fields}), flush=True)

    steps = scenario.get(
        "resume" if options.session and "resume" in scenario else "steps",
        [{"text": "one"}, {"text": "two"}],
    )
    outputs = []
    for step in steps:
        if "text" in step:
            outputs.append(("text", {"part": {"type": "text", "text": step["text"]}}))
        elif "tool" in step:
            outputs.append(
                ("tool_use", {"part": {"tool": step["tool"], "state": {"status": "completed"}}})
            )
        elif "bash" in step:
            state = {"status": "completed"}
            if not allowed(config, step["bash"], auto=options.auto):
                refused.append(step["bash"])
                state = {"status": "error", "error": REJECTED}
            outputs.append(("tool_use", {"part": {"tool": "bash", "state": state}}))
        else:
            outputs.append((None, step))
    with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as stream:
        call = {
            "argv": args,
            "cwd": os.getcwd(),
            "prompt": prompt,
            "session": options.session,
            "auto": options.auto,
            "config": config,
            "refused": refused,
            "env": {key: os.environ.get(key) for key in ENV},
        }
        stream.write(json.dumps(call) + "\n")
    emit("step_start", part={"type": "step-start"})
    for kind, fields in outputs:
        if kind is not None:
            emit(kind, **fields)
        elif "error" in fields:
            emit("error", error=fields["error"])
        elif "err" in fields:
            print(fields["err"], file=sys.stderr, flush=True)
        elif "sleep" in fields:
            time.sleep(fields["sleep"])
        elif "exit" in fields:
            return fields["exit"]
        else:
            raise RuntimeError("unknown fake step")
    emit("step_finish", part={"reason": "stop"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
