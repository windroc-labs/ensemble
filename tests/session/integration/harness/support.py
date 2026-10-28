"""recording_handler's registration and its turn records, for the harness integration tests."""

from ensemble.session.engine.types import HookCallback
from tests.session.case import REPO
from tests.session.live_tmux import logged

HANDLER = "tests.session.integration.harness.recording_handler"
TURNS = ("TurnStarted", "TurnFailed", "TurnEnd", "Exited")


class Recording:
    """The handler's records in self.out."""

    out: str

    def handlers(self) -> tuple[HookCallback, ...]:
        return (HookCallback(HANDLER, {"path": self.out}, (REPO,)),)

    def handled(self) -> list[dict]:
        return [r for r in logged(self.out) if r["type"] in TURNS]
