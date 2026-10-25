"""Shared test fixtures and support."""

import io


class Stderr(io.StringIO):
    def __init__(self, fail=None, *, tty=False):
        super().__init__()
        self.tty, self.fail = tty, fail

    def isatty(self):
        return self.tty

    def write(self, s):
        if self.fail:
            raise self.fail
        return super().write(s)
