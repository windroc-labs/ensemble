"""An executable fake for orphan listing, backed by explicit Record fixtures."""

import json
import shlex
import sys
from pathlib import Path

from ensemble.utils import Record, append_line, save


def install(bin_dir: str, state_dir: str) -> None:
    Path(state_dir).mkdir(exist_ok=True)
    set_records(state_dir, ())
    utils_src = Path(__file__).resolve().parents[2] / "utils" / "src"
    argv = ("env", f"PYTHONPATH={utils_src}", sys.executable, "-P", "-S", __file__, state_dir)
    command = Path(bin_dir, "ensemble-session")
    save(command, f'#!/bin/sh\nexec {shlex.join(argv)} "$@"\n')
    command.chmod(0o755)


def set_records(state_dir: str, records) -> None:
    save(Path(state_dir, "records.jsonl"), "".join(record.to_line() + "\n" for record in records))


def main(state_dir: str, argv: list[str]) -> int:
    append_line(Path(state_dir, "calls.jsonl"), json.dumps(argv) + "\n")
    if argv not in (["ls", "--here", "--records"], ["ls", "--here", "--records", "--descendants"]):
        return 2
    for line in Path(state_dir, "records.jsonl").read_text().splitlines():
        print(Record.parse(line).to_line())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2:]))
