"""Standard-library exception fixtures without importing launch APIs into unit tests."""

from subprocess import DEVNULL, PIPE, STDOUT, TimeoutExpired

__all__ = ["DEVNULL", "PIPE", "STDOUT", "TimeoutExpired"]
