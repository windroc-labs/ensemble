"""A test HOME's process controllers retired before its cleanup removes what they write."""

import glob
import os
import time

from ensemble.utils import ProcessControl

WAIT = 30  # s: a kill, then process_control.TIMEOUT for a group no client accepted
POLL = 0.05


def retired(root: str, wait: float = WAIT) -> None:
    """Each controller serving under root, its drive killed if running, awaited until it removed
    its own directory: it records done first, so nothing writes there after.
    """
    serving = []
    for path in sorted(glob.glob(os.path.join(root, "apm-group-*"))):
        control = ProcessControl.attach(path)
        if control.ended:
            continue
        try:
            if control.running():
                control.kill()
            else:
                control.live()
        except ConnectionRefusedError:  # its controller was killed: nothing writes there
            continue
        serving.append(path)
    deadline = time.monotonic() + wait
    while left := [path for path in serving if os.path.isdir(path)]:
        if time.monotonic() > deadline:
            raise AssertionError(f"process controllers still running after {wait}s: {left}")
        time.sleep(POLL)
