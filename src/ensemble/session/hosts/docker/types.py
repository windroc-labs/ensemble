"""Private parsed broker responses for the Docker Host."""

import re
from collections.abc import Mapping
from dataclasses import dataclass

GEN = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True)
class Started:
    container: str
    endpoint: str
    image_digest: str
    clients: Mapping[str, str]  # client name → its runnable version
    env: Mapping[str, str]  # broker-provided execution environment
    deadline: Mapping[str, str] | None


@dataclass(frozen=True)
class Maintained:
    results: list[dict]  # each {"credential", "expires_at", "error"}
    next_check: str | None
    deadline: Mapping[str, str] | None  # {"at", "action", "reason"}
