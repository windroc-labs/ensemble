"""The harness: `python3 -P -s -m ensemble.delegate.engine.harness <workdir>`, a detached run's own
process on its harness Host. It attaches to the run by its workdir, drives it to its Result and
appends the run's one events outcome line.

Exits 0 on a valid outcome, 1 without one, 75 when its start found its lock held (error `busy: …`),
2 when the run is refused (usage, its state, config), 128 + n on signal n. Host.detach starts it
with SIGNALS blocked; they are unblocked only while the run executes, so a late signal cannot drop
the outcome line. The first stop signal ignores later ones instead of blocking them, since a
callback started afterwards would inherit a blocked mask.
"""

import signal
import sys

from ensemble.delegate.engine.harness_delegate import HarnessDelegate
from ensemble.delegate.engine.runner import report, say
from ensemble.delegate.engine.types import Begun, ConfigError, Event, Result, Stopped
from ensemble.session import SIGNALS


def main(argv: list[str], *, delegate_cls: type[HarnessDelegate] = HarnessDelegate) -> int:
    if len(argv) != 1:
        say("usage: python3 -m ensemble.delegate.engine.harness WORKDIR")
        return 2
    try:
        owner = delegate_cls.attach(argv[0])
    except ConfigError as error:
        say(f"delegate: {error}")
        return 2
    try:
        result = _run(owner)
    except Stopped as stopped:
        return stopped.code
    except ConfigError as error:
        say(f"delegate: {error}")
        return 2
    return report(result, None)[0]


def _run(owner: HarnessDelegate) -> Result:
    """The run with SIGNALS unblocked; blocked again before its outcome line."""
    result: Result | None = None
    for signum in SIGNALS:
        signal.signal(signum, _stop)
    try:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, SIGNALS)
        result = owner.run(_said)
        return result
    finally:
        signal.pthread_sigmask(signal.SIG_BLOCK, SIGNALS)
        owner.announce(result)


def _said(event: Event) -> None:
    if isinstance(event, Begun):
        say(f"delegate: session {event.sid}")


def _stop(signum, _frame):
    for each in SIGNALS:
        # not SIG_IGN: exec keeps an ignored disposition, it resets a handled one
        signal.signal(each, _ignore)
    raise Stopped(128 + signum)


def _ignore(_signum, _frame):
    pass


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
