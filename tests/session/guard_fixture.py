"""Credential-free client tree and minimal harness for managed-worker crash tests."""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import uuid
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
    descendant = subprocess.Popen(
        [sys.executable, "-I", __file__, "child", directory, *(["--daemon"] if daemon else [])],
        start_new_session=True,
        stdout=subprocess.PIPE,
    )
    assert descendant.stdout is not None
    try:
        if descendant.stdout.readline() != b"ready\n":
            raise RuntimeError("child ended before ready")
    finally:
        descendant.stdout.close()
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
    print("ready", flush=True)
    while True:
        time.sleep(1)


def colors(directory: str) -> None:
    _publish(Path(directory, "display.pid"), str(os.getpid()))
    for number in range(80):
        print(f"\x1b[31mstartup-{number:03}\x1b[0m", flush=True)
    for line in sys.stdin:
        print(f"\x1b[32m{line.strip()}\x1b[0m", flush=True)


def history(directory: str, count: int, long: int) -> None:
    """long lines of 200+ characters with spinner redraws after them, count colored numbered lines,
    then a prompt; `emit N TAG` prints N lines, others echo.
    """
    _publish(Path(directory, "display.pid"), str(os.getpid()))
    sys.stdout.write(
        "".join(f"long-{n}:" + chr(ord("a") + n) * (200 + n) + "\n" for n in range(long))
    )
    if long:
        sys.stdout.write("spin\n" + "".join(f"\x1b[1A\r\x1b[2Kspinner-{n}\n" for n in range(10)))
    sys.stdout.write("".join(f"\x1b[3{1 + n % 6}mline-{n:04}\x1b[0m\n" for n in range(count)))
    print("\x1b[36m> \x1b[0m", end="", flush=True)
    for line in sys.stdin:
        words = line.split()
        if words[:1] == ["emit"]:
            print("".join(f"{words[2]}-{n:04}\n" for n in range(int(words[1]))), end="")
        else:
            print(f"\x1b[32m{line.strip()}\x1b[0m")
        print("\x1b[36m> \x1b[0m", end="", flush=True)


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
        metadata={"generation": uuid.uuid4().hex, "sid": "same-conversation"},
    )
    _publish(
        Path(directory, "harness.json"),
        json.dumps({"guard": worker.proc.pid, "generation": worker.generation}),
    )
    while True:
        time.sleep(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("harness", "client", "child", "colors", "history"))
    parser.add_argument("directory")
    parser.add_argument("--terminal", action="store_true")
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--count", type=int, default=0)
    parser.add_argument("--long", type=int, default=0)
    args = parser.parse_args()
    if args.kind == "harness":
        harness(args.directory, terminal=args.terminal, daemon=args.daemon)
    elif args.kind == "client":
        client(args.directory, daemon=args.daemon)
    elif args.kind == "colors":
        colors(args.directory)
    elif args.kind == "history":
        history(args.directory, args.count, args.long)
    else:
        child(args.directory, daemon=args.daemon)


if __name__ == "__main__":
    main()
