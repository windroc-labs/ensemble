#!/usr/bin/env python3
"""A session CLI process boundary: records references and returns a worker Record or busy."""

import argparse
import json
import os
import sys
import uuid
from pathlib import Path


def main():
    assert sys.argv[1:6] == ["-P", "-s", "-m", "ensemble.session.cli", "start"]
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--worker-name", dest="name", required=True)
    for option in ("host", "client", "cwd", "environment-spec", "account", "display"):
        parser.add_argument(f"--{option}")
    args = parser.parse_args(sys.argv[6:])
    sid = os.environ.get("ENSEMBLE_SID") or str(uuid.uuid4())
    Path(os.environ["FAKE_SESSION_LOG"]).write_text(
        json.dumps(
            {
                "argv": sys.argv[1:],
                "sid": sid,
                "events": os.environ.get("ENSEMBLE_EVENTS"),
            }
        )
    )
    code = int(os.environ.get("FAKE_SESSION_EXIT", "0"))
    if code:
        print("busy: lock dummy-lock is held by dummy-1 (owner dummy-1)", file=sys.stderr)
        return code
    print(
        json.dumps(
            {
                "v": 1,
                "kind": "worker",
                "name": args.name,
                "sessions": [args.name],
                "sid": sid,
                "workdir": str(Path.home() / ".ensemble" / "workers" / sid),
                "ctl": ["ensemble-session"],
                "resume": ["ensemble-session", "resume", args.name],
                "display": args.display,
                "detail": args.client,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
