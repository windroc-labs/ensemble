"""The runner's unit fakes: fake client steps (said, limit), session events (turn, STOP),
FakeHandle and no_child.
"""

import contextlib
import os
from unittest import mock

from ensemble.delegate.engine.types import Result
from ensemble.session import Exited, TurnEnd
from ensemble.utils import Status, StatusState
from tests.session.case import SPAWNS

NAME = "fake-11111111"
DONE = {"status": "done", "title": "T", "summary": "S", "deliverable": "# Doc\n"}
START = "[ensemble-progress:start] go"
GAVE_UP = Result(0, None, "the agent run returned no outcome")


def said(*lines) -> dict:
    """A step printing a message record (Reply) of `lines`."""
    return {"out": {"type": "message", "text": "\n".join(lines)}}


def limit(resets_at) -> dict:
    return {"out": {"type": "limit", "resets_at": resets_at}}


@contextlib.contextmanager
def no_child():
    """Each of SPAWNS fails inside, and none was called."""
    with contextlib.ExitStack() as stack:
        spawns = [
            stack.enter_context(mock.patch(target, side_effect=AssertionError(target)))
            for target in SPAWNS
        ]
        yield
    for spawn in spawns:
        spawn.assert_not_called()


def turn(*lines, pending=0) -> TurnEnd:
    return TurnEnd("\n".join(lines), pending)


STOP = turn("Working.")


class FakeHandle:
    """ensemble.session.Session as the Delegate uses it: events() goes on through `events` across
    calls, up to an Exited (none left: AssertionError); send (raising `fail`) and stop (raising
    `stop_fail`) recorded in `calls`; show's commands (raising `show_fail`) in `shown`; status: live,
    `hidden` as set. start: the Delegate Driver's stand-in.
    """

    showable = True
    show_fail: Exception | None = None
    hidden = False

    def __init__(self, events, fail=None, stop_fail=None):
        self.left, self.fail, self.stop_fail, self.calls = iter(events), fail, stop_fail, []
        self.shown: list[str | None] = []
        self.exhausted = AssertionError("no event left")
        self.name, self.sid = NAME, None

    def start(self, spec, *, display=None, host=None, idle_timeout=None):
        spec = spec.session
        os.makedirs(spec.workdir, exist_ok=True)
        self.name, self.sid = spec.name or NAME, spec.sid
        return self

    def attach_command(self):
        return f"tmux attach -t '={self.name}'"

    def status(self):
        return Status(
            name=self.name,
            alive=True,
            state=StatusState.WORKING,
            outcome=None,
            attach=self.attach_command(),
            hidden=self.hidden,
        )

    def show(self, *, display=None, command=None):
        self.shown.append(command)
        if self.show_fail:
            raise self.show_fail
        return None

    def events(self):
        for event in self.left:
            yield event
            if isinstance(event, Exited):
                return
        raise self.exhausted

    def send(self, text):
        self.calls.append(("send", text))
        if self.fail:
            raise self.fail

    def stop(self):
        self.calls.append(("stop",))
        if self.stop_fail:
            raise self.stop_fail
