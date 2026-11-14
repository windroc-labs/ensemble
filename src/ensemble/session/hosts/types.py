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
    """printed: the attach command shown; argv: what the Viewer runs to show it; run: names the
    worker run it shows, unchanged when the attach program moves (empty: printed).
    """

    printed: str
    argv: tuple[str, ...]
    run: str = ""

    def __post_init__(self) -> None:
        if not self.run:
            object.__setattr__(self, "run", self.printed)
