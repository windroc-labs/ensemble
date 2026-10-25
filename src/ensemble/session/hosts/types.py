from dataclasses import dataclass


@dataclass(frozen=True)
class SessionView:
    """An existing session the Viewer places as it is."""

    session: str


@dataclass(frozen=True)
class CommandView:
    """printed: the attach command shown; argv: what the view session runs."""

    printed: str
    argv: tuple[str, ...]
