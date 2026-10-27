"""Reply and run-record helpers shared by delegate tests and the layers above."""

import json
import os


def completed_steps(steps) -> list[dict]:
    """A successful fake headless script includes its native terminal record."""
    steps, last = list(steps), ""
    for step in steps:
        output = step.get("out", {})
        if "exit" in step or output.get("type") == "result":
            return steps
        if output.get("type") == "message":
            last = output["text"]
    return [*steps, {"out": {"type": "result", "text": last}}]


def block(data) -> str:
    return f"```ensemble-outcome\n{json.dumps(data)}\n```"


def mark(name: str, text: str) -> str:
    return f"[ensemble-progress:{name}] {text}"


def prompt(call: dict) -> str:
    return call["argv"][-1]


def option(call: dict, name: str) -> str | None:
    argv = call["argv"]
    return argv[argv.index(name) + 1] if name in argv else None


def bare(record: dict) -> dict:
    return {k: v for k, v in record.items() if k != "ts"}


def running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
