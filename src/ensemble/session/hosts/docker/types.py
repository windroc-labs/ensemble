"""Private parsed broker responses for the Docker Host."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Started:
    container: str
    endpoint: str
    image_digest: str
    clients: Mapping[str, str]  # client name → its runnable version
    github: Sequence[Mapping[str, str]]  # each GitHub token's {"account", "expires_at"}
    github_user_id: int | None  # the App's bot user ID; None without an App
    credential_expires_at: str | None


@dataclass(frozen=True)
class Refreshed:
    github: list[dict]
    credential_expires_at: str | None


@dataclass(frozen=True)
class Noticed:
    notices: list[dict]  # each {"key", "text"}: a static secret notice the session is due
    next_check: str | None  # when to check again; None: never
