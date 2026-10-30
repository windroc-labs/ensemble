from dataclasses import dataclass


@dataclass(frozen=True)
class WorkerState:
    sid: str | None
    generation: str | None
    client: str | None
    display: str | None
    owner: str | None
    host: str | None = None


@dataclass(frozen=True)
class CommandView:
    """printed: the attach command shown; argv: what the view session runs."""

    printed: str
    argv: tuple[str, ...]
