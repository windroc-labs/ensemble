"""Credential-free client tree and minimal harness for managed-worker crash tests."""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def _publish(path, text: str) -> None:
    """path holding text whole once it exists."""
    temporary = f"{path}.{os.getpid()}.tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        stream.write(text)
    os.replace(temporary, path)


def client(directory: str, *, daemon: bool) -> None:
    root = Path(directory)
    record = root / "transcript.json"
    if not record.exists():
        record.write_text(json.dumps({"sid": "same-conversation", "phrase": "violet compass"}))
    subprocess.Popen(
        [sys.executable, "-I", __file__, "child", directory, *(["--daemon"] if daemon else [])],
        start_new_session=True,
    )
    _publish(root / "client.pid", str(os.getpid()))
    for line in sys.stdin:
        if line.strip() == "recall":
            print(json.loads(record.read_text())["phrase"], flush=True)
        elif line.strip() == "exit":
            print("final reply", flush=True)
            return
        elif line.strip() == "flood":
            while True:
                os.write(1, b"x" * 65536)


def child(directory: str, *, daemon: bool) -> None:
    if daemon:
        if os.fork():
            return
        os.setsid()
        if os.fork():
            return
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    _publish(Path(directory, "child.pid"), str(os.getpid()))
    while True:
        time.sleep(1)


def harness(directory: str, *, terminal: bool, daemon: bool) -> None:
    from ensemble.session.engine.types import Launch
    from ensemble.session.hosts.tmux import TmuxHost

    worker = TmuxHost("tmux").worker(
        "managed-worker",
        Launch(
            (
                sys.executable,
                "-I",
                __file__,
                "client",
                directory,
                *(["--daemon"] if daemon else []),
            ),
            cwd=directory,
        ),
        terminal=terminal,
        metadata={"sid": "same-conversation"},
    )
    _publish(
        Path(directory, "harness.json"),
        json.dumps({"guard": worker.proc.pid, "generation": worker.generation}),
    )
    while True:
        time.sleep(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("harness", "client", "child"))
    parser.add_argument("directory")
    parser.add_argument("--terminal", action="store_true")
    parser.add_argument("--daemon", action="store_true")
    args = parser.parse_args()
    if args.kind == "harness":
        harness(args.directory, terminal=args.terminal, daemon=args.daemon)
    elif args.kind == "client":
        client(args.directory, daemon=args.daemon)
    else:
        child(args.directory, daemon=args.daemon)


if __name__ == "__main__":
    main()
