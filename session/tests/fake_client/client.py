"""The fake client: client.py --scenario FILE --log FILE --sid SID [--resume] -- PROMPT [HOOK...].

Logs its argv, pid, cwd and env (PWD, FAKE_ENV, PYTHONPATH) as a JSON line, then runs the scenario,
a JSON list of steps (or {"steps": [...], "resume": [...]}: resume's run on --resume): {"out": obj}
prints obj as JSON, {"raw": text} prints text, {"err": text} to stderr, {"sleep": s}, {"exit": n},
{"hook": label, "payload": obj} runs HOOK + [label] with obj on stdin, {"read": true} reads a stdin
line, {"run": argv} runs argv quietly and logs {ran, found (PATH's argv[0]), rc}; then exits 0.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser(prog="client.py")
    for flag in ("--scenario", "--log", "--sid"):
        parser.add_argument(flag, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("prompt")
    parser.add_argument("hook", nargs="*")
    args = parser.parse_args()
    with open(args.log, "a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "argv": sys.argv[1:],
                    "pid": os.getpid(),
                    "cwd": os.getcwd(),
                    "env": {k: os.environ.get(k) for k in ("PWD", "FAKE_ENV", "PYTHONPATH")},
                }
            )
            + "\n"
        )
    with open(args.scenario, encoding="utf-8") as f:
        scenario = json.load(f)
    if isinstance(scenario, list):
        scenario = {"steps": scenario}
    steps = (
        scenario["resume"] if args.resume and "resume" in scenario else scenario.get("steps", [])
    )
    return execute(steps, args.hook, args.log)


def execute(steps, hook, log):
    for step in steps:
        if "out" in step:
            print(json.dumps(step["out"]), flush=True)
        elif "raw" in step:
            print(step["raw"], flush=True)
        elif "err" in step:
            print(step["err"], file=sys.stderr, flush=True)
        elif "sleep" in step:
            time.sleep(step["sleep"])
        elif "exit" in step:
            return step["exit"]
        elif "hook" in step:
            subprocess.run(
                [*hook, step["hook"]], input=json.dumps(step.get("payload", {})), text=True
            )
        elif "read" in step:
            sys.stdin.readline()
        elif "run" in step:
            argv = step["run"]
            done = subprocess.run(argv, capture_output=True, stdin=subprocess.DEVNULL)
            with open(log, "a", encoding="utf-8") as f:
                f.write(
                    json.dumps({"ran": argv, "found": shutil.which(argv[0]), "rc": done.returncode})
                    + "\n"
                )
    return 0


if __name__ == "__main__":
    sys.exit(main())
