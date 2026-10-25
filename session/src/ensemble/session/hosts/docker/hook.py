"""The Docker Host's callback for tui hooks: marks the session in its container through docker exec
and appends its events line on the host.
"""

import os

from ensemble.session.engine import tmux_cli
from ensemble.session.engine.types import HookCall, HookError
from ensemble.session.hosts.docker.daemon import container_name, endpoint, exec_runner
from ensemble.session.hosts.tmux.hook import mark_launched


def on_hook(word: str, call: HookCall) -> None:
    saved = call.state
    docker, target, generation = (saved.get(key) for key in ("docker", "target", "generation"))
    if not isinstance(docker, str) or not os.path.isabs(docker):
        raise HookError("host state: bad docker")
    if not isinstance(target, str) or not tmux_cli.NAME.fullmatch(target):
        raise HookError("host state: bad target")
    if not isinstance(generation, str) or not generation.isalnum():
        raise HookError("host state: bad generation")
    if saved.get("endpoint") != endpoint():
        raise HookError("host state: bad endpoint")
    container = container_name(target, generation)
    if saved.get("container") != container:
        raise HookError("host state: bad container")
    mark_launched(
        exec_runner(docker, container),
        word,
        call,
        target=target,
        generation=generation,
        tmux="tmux",
        session=target,
    )
