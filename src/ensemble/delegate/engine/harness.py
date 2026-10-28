"""Detached drive; owned handlers write outcome independently of its exited line.
Other runs write outcome first. Exit: 0 valid, 1 invalid, 75 busy, 2 refused, 128+n signal.
SIGNALS stay blocked outside the run; late signals cannot drop its notifications.
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
    rc = 1
    try:
        rc = _drive(owner)
        return rc
    finally:
        owner._exited(rc)


def _drive(owner: HarnessDelegate) -> int:
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
        # exec resets a handled disposition, but keeps SIG_IGN
        signal.signal(each, _ignore)
    raise Stopped(128 + signum)


def _ignore(_signum, _frame):
    pass


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
