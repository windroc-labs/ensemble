#!/usr/bin/env python3
"""A docker CLI for reader service tests: state volumes are dirs under
$HOME/.ensemble/docker-state, owned 1001:1002; `run` logs its pid to $HOME/docker-runs and becomes
state_reader.py over that dir, whatever program it was given.
"""

import json
import os
import sys

READER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    *("src", "ensemble", "session", "engine", "state_reader.py"),
)


def main(argv: list[str]) -> int:
    args = argv[2:] if argv[:1] == ["--host"] else argv
    home = os.environ["HOME"]
    root = os.path.join(home, ".ensemble", "docker-state")
    if args[:2] == ["volume", "ls"]:
        for index, arg in enumerate(args):
            volume = args[index + 1].removeprefix("name=") if arg == "--filter" else ""
            if volume and os.path.isdir(os.path.join(root, volume.removeprefix("ensemble-state-"))):
                print(volume)
        return 0
    if args[:2] == ["volume", "inspect"]:
        for volume in args[args.index("--format") + 2 :]:
            session = volume.removeprefix("ensemble-state-")
            print(json.dumps({"ensemble.state": session, "ensemble.state-owner": "1001:1002"}))
        return 0
    if args[:1] == ["run"]:
        with open(os.path.join(home, "docker-runs"), "a", encoding="utf-8") as f:
            f.write(f"{os.getpid()}\n")
        os.execv(sys.executable, [sys.executable, "-I", READER, root])
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
