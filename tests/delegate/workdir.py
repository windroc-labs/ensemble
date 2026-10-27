"""Workdir paths other projects' tests need; the layout stays delegate's."""

import os

from ensemble.delegate.engine.types import Layout
from ensemble.utils import checkout


def session_cwd(workdir: str) -> str:
    return Layout(workdir).agent


def checkout_root(workdir: str) -> str:
    return os.path.join(Layout(workdir).agent, "src")


def checkout_path(workdir: str, owner: str, name: str, slug: str) -> str:
    return checkout(checkout_root(workdir), owner, name, slug)


def run_log(workdir: str) -> str:
    return os.path.join(Layout(workdir).run, "run.jsonl")


RACED = ("client_session", "session_metadata", "stderr")  # noted by the session harness


def input_first(records: list[dict]) -> list[dict]:
    """A session harness notes its worker's RACED rows in its own process as the run notes its
    input once the start returns; no reader orders them: the raced rows are put after that input.
    """
    found: list[dict] = []
    for record in records:
        at = len(found)
        while record["kind"] == "input" and at and found[at - 1]["kind"] in RACED:
            at -= 1
        found.insert(at, record)
    return found
