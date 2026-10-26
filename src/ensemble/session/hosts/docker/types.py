"""Private parsed broker responses for the Docker Host."""

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Started:
    container: str
    endpoint: str
    image_digest: str
    clients: Mapping[str, str]  # client name → its runnable version
    github_user_id: int | None  # the App's bot user ID; None without an App
    deadline: Mapping[str, str] | None


@dataclass(frozen=True)
class Maintained:
    results: list[dict]  # each {"credential", "expires_at", "error"}
    notices: list[dict]
    next_check: str | None
    deadline: Mapping[str, str] | None  # {"at", "action", "reason"}
