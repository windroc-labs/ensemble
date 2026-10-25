"""Capture failures from one worker through the standard thread exception hook."""

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
