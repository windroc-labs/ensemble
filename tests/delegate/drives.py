"""A test's detached drives ended before its cleanup removes what they write."""

import contextlib
import json
import os
import time
from pathlib import Path

from ensemble.utils import ProcessControl
from ensemble.utils.process_control import POLL

WAIT = 30  # s
KILL_WAIT = 10  # s


def end(root: str, wait: float = WAIT) -> None:
    """End each `<root>/**/run/delegate.json` drive; its controller dir goes last (serve)."""
    found = (_control(path) for path in sorted(Path(root).glob("**/run/delegate.json")))
    controls = [control for control in found if control is not None]
    for control in controls:
        with contextlib.suppress(OSError):
            control.terminate()
    if stuck := [control.path for control in controls if not _ended(control, wait)]:
        raise AssertionError(f"drives still running after {wait}s, then killed: {stuck}")


def stop(control: ProcessControl, wait: float = WAIT) -> None:
    with contextlib.suppress(OSError):
        control.terminate()
    if not _ended(control, wait):
        raise AssertionError(f"drive still running after {wait}s, then killed: {control.path}")


def _control(path: Path) -> ProcessControl | None:
    try:
        drive = json.loads(path.read_text(encoding="utf-8")).get("drive")
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
