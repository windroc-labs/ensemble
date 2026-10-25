"""Guards for subprocess commands forbidden in a test."""

import subprocess
from unittest import mock


def reject_command(command):
    run = subprocess.run

    def checked(argv, *args, **kwargs):
        if argv[0] == command:
            raise AssertionError(f"unexpected command: {command}")
        return run(argv, *args, **kwargs)

    return mock.patch("subprocess.run", checked)
