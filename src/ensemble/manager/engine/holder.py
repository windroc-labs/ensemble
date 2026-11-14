"""Keep a roster lease while its manager command is running."""

import contextlib
import os
import re
import signal
import subprocess
import uuid

from ensemble.manager.engine.roster import Roster
from ensemble.manager.engine.types import Lease
from ensemble.utils import SID_ENV


def inherited() -> Lease | None:
    """The holder pair this process inherited from its hold; None without one."""
    holder = os.environ.get("ENSEMBLE_HOLDER_ID")
    generation = os.environ.get("ENSEMBLE_HOLDER_GENERATION")
    if holder is None and generation is None:
        return None
    if not holder or generation is None or not re.fullmatch(r"[1-9][0-9]*", generation):
        raise ValueError("ENSEMBLE_HOLDER_ID and ENSEMBLE_HOLDER_GENERATION must go together")
    identity = uuid.UUID(holder)
    if identity.version != 4 or str(identity) != holder:
        raise ValueError("ENSEMBLE_HOLDER_ID must be a canonical UUID4")
    return Lease(holder, int(generation))


def _foreground(group: int) -> None:
    previous = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    try:
        os.tcsetpgrp(0, group)
    finally:
        signal.signal(signal.SIGTTOU, previous)


class Holder:
    def __init__(self, roster: Roster):
        self.roster = roster

    def run(
        self, argv: tuple[str, ...], *, session: str, force: bool = False
    ) -> int:  # pragma: no cover
        if not argv or not all(isinstance(word, str) for word in argv) or not argv[0]:
            raise ValueError("hold requires a nonempty manager command")
        with self.roster.hold(session, force=force) as lease:
            env = dict(os.environ)
            env.pop(SID_ENV, None)
            env.update(
                ENSEMBLE_MANAGER=os.path.basename(self.roster.directory),
                ENSEMBLE_HOLDER_ID=lease.id,
                ENSEMBLE_HOLDER_GENERATION=str(lease.generation),
                ENSEMBLE_HOLDER_PID=str(os.getpid()),
            )
            child: subprocess.Popen | None = None
            pending: list[int] = []
            foreground = os.tcgetpgrp(0) if os.isatty(0) else None

            def forward(signum, _frame):
                if child is None:
                    pending.append(signum)
                elif child.poll() is None:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(child.pid, signum)

            previous = {
                signum: signal.signal(signum, forward) for signum in (signal.SIGINT, signal.SIGTERM)
            }
            if foreground is not None:
                # tmux starts panes with these ignored; then the child's terminal read before the
                # handoff below fails with EIO instead of stopping until the SIGCONT after it
                previous |= {
                    signum: signal.signal(signum, signal.SIG_DFL)
                    for signum in (signal.SIGTTIN, signal.SIGTTOU)
                }
            try:
                child = subprocess.Popen(argv, env=env, close_fds=True, process_group=0)
                if foreground is not None:
                    try:
                        _foreground(child.pid)
                    except OSError:
                        if child.poll() is None:
                            raise
                    else:
                        forward(signal.SIGCONT, None)
                for signum in pending:
                    forward(signum, None)
                returncode = child.wait()
                return returncode if returncode >= 0 else 128 - returncode
            except OSError:
                forward(signal.SIGCONT, None)
                forward(signal.SIGTERM, None)
                raise
            finally:
                if child is not None:
                    child.wait()
                try:
                    if (
                        foreground is not None
                        and child is not None
                        and os.tcgetpgrp(0) in (0, child.pid)
                    ):
                        _foreground(foreground)
                finally:
                    for signum, handler in previous.items():
                        signal.signal(signum, handler)
