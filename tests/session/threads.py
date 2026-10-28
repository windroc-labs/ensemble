"""Capture failures from one worker through the standard thread exception hook; drain events."""

import threading
from contextlib import contextmanager
from unittest import mock


@contextmanager
def capture_errors(thread, receive):
    previous = threading.excepthook

    def capture(args):
        if args.thread is thread:
            receive(args.exc_value)
        else:
            previous(args)

    with mock.patch("threading.excepthook", new=capture):
        yield


def drain(case, handle, on=lambda _event: None, wait=20) -> list:
    """handle.events() to its end, each passed to on(event); fails, stopping it, past `wait` s."""
    case.addCleanup(handle.stop)
    found, errors = [], []

    def run():
        for event in handle.events():
            found.append(event)
            on(event)

    thread = threading.Thread(target=run, daemon=True)
    with capture_errors(thread, errors.append):
        thread.start()
        thread.join(wait)
    if thread.is_alive():
        handle.stop()
        case.fail(f"events() did not end within {wait}s, got {found}")
    if errors:
        raise errors[0]
    return found
