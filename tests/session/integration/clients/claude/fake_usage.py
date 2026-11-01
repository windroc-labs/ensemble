"""A stand-in `claude` whose `-p ... /usage` answers its fake_claude scenario's usage panel, logged
as fake_claude logs a start; any other invocation runs fake_claude.
"""

import json
import os
import shlex
import sys
from pathlib import Path

FAKE = Path(__file__).with_name("fake_claude.py")
ENV = "FAKE_CLAUDE_SCENARIO"
UNUSED = "Current session: 0% used\n"


def install(bin_dir: str, tail: str = "/usr/bin:/bin") -> str:
    path = os.path.join(bin_dir, "claude")
    with open(path, "w", encoding="utf-8") as f:
        q = shlex.quote
        f.write(f'#!/bin/sh\nexec {q(sys.executable)} -I {q(os.path.abspath(__file__))} "$@"\n')
    os.chmod(path, 0o755)
    return f"{bin_dir}:{tail}"


def scenario() -> dict:
    try:
        found = json.loads(Path(os.environ[ENV]).read_text(encoding="utf-8"))
    except (KeyError, OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def main(argv: list[str]) -> int:
    if argv[:1] != ["-p"] or argv[-1:] != ["/usage"]:
        os.execv(sys.executable, [sys.executable, "-I", str(FAKE), *argv])
    s, config_dir = scenario(), os.environ.get("CLAUDE_CONFIG_DIR", "")
    if s.get("log"):
        with open(s["log"], "a", encoding="utf-8") as f:
            entry = {"pid": os.getpid(), "argv": argv, "cwd": os.getcwd(), "config_dir": config_dir}
            f.write(json.dumps(entry) + "\n")
    usage = s.get("usage", UNUSED)
    if isinstance(usage, dict):
        usage = usage.get(config_dir, UNUSED)
    print(usage, end="", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
