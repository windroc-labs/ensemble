"""The Docker Host's callback for a harness-owned worker's tui hooks: its activity word and, for
done and blocked, its events line, on the host; no docker call.
"""

import re

from ensemble.session.engine.types import HookCall, HookError
from ensemble.session.hosts.docker.mounts import state_dir
from ensemble.session.hosts.docker.worker import write_activity

TARGET = re.compile(r"[A-Za-z0-9_-]+")


def on_hook(word: str, call: HookCall) -> None:
    target, generation = call.state.get("target"), call.state.get("generation")
    if not isinstance(target, str) or not TARGET.fullmatch(target):
        raise HookError("host state: bad target")
    if not isinstance(generation, str) or not generation.isalnum():
        raise HookError("host state: bad generation")
    try:
        write_activity(state_dir(target), generation, word, call.events, target)
    except (OSError, ValueError) as error:
        raise HookError(f"docker session {target}: {error}") from None
