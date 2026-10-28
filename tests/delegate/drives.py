"""A test's detached drives ended before its cleanup removes what they write."""

import contextlib
import json
import os
import time

from ensemble.utils import ProcessControl

WAIT = 30  # s
KILL_WAIT = 10  # s
POLL = 0.05  # s


def end(root: str, wait: float = WAIT) -> None:
    """End each `<root>/**/run/delegate.json` drive; its controller dir goes last (serve)."""
    found = (
        _control(os.path.join(top, "delegate.json"))
        for top, _, names in sorted(os.walk(root))  # a vanished subtree is skipped
        if os.path.basename(top) == "run" and "delegate.json" in names
    )
    _stop([control for control in found if control is not None], wait)


def stop(control: ProcessControl, wait: float = WAIT) -> None:
    _stop([control], wait)


def _stop(controls: list[ProcessControl], wait: float) -> None:
    for control in controls:
        with contextlib.suppress(OSError):
            control.terminate()
    if stuck := [control.path for control in controls if not _ended(control, wait)]:
        raise AssertionError(f"drives still running after {wait}s, then killed: {stuck}")


def _control(path: str) -> ProcessControl | None:
    try:
        with open(path, encoding="utf-8") as f:
            drive = json.load(f).get("drive")
        control = ProcessControl.attach(drive) if isinstance(drive, str) else None
    except (OSError, ValueError, AttributeError):
        return None
    return None if control is None or control.ended else control


def _ended(control: ProcessControl, wait: float) -> bool:
    if _gone(control.path, wait):
        return True
    with contextlib.suppress(OSError):
        control.kill()
    _gone(control.path, KILL_WAIT)
    return False


def _gone(path: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while os.path.isdir(path):
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL)
    return True
