"""Fakes shared by the guard channel's host-end tests."""

import contextlib
import os
import select
from unittest import mock


@contextlib.contextmanager
def writable(write=lambda _fd, data: len(data)):
    """Guard input always writable, `os.write` running `write`; yields the `os.write` mock."""
    with (
        mock.patch.object(select, "select", return_value=([], [1], [])),
        mock.patch.object(os, "write", side_effect=write) as written,
    ):
        yield written
