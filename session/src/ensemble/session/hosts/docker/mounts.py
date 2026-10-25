"""The workspace volume name and a session's state dir. The broker owns the mount-point rules
(broker/internal/launcher/mounts.go).
"""

import os

from ensemble.utils import docker_home

KIND_LABEL = "ensemble.kind"  # a workspace volume's labels, set by the broker
WORKSPACE_LABEL = "ensemble.workspace"


def workspace_volume(name: str) -> str:
    """Session `name`'s workspace volume, kept across its restarts and resumes."""
    return f"ensemble-ws-{name}"


def state_dir(name: str) -> str:
    """Session `name`'s dir under DOCKER_STATE: its client home, manifests and supervisor files."""
    return os.path.normpath(docker_home(name, os.curdir))
