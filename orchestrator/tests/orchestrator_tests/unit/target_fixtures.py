"""Shared test fixtures and support."""

import json
from types import SimpleNamespace


def ok(stdout="", code=0, stderr=""):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)


class FakeRun:
    def __init__(self, table):
        self.table, self.calls = table, []

    def __call__(self, argv, timeout):
        self.calls.append((tuple(argv), timeout))
        for prefix, res in self.table:
            if tuple(argv[: len(prefix)]) == prefix:
                if isinstance(res, BaseException):
                    raise res
                return res
        raise AssertionError(f"unexpected {argv}")


def api(full_name="acme/ensemble", default="main", *, push=True):
    return ok(
        json.dumps(
            {"full_name": full_name, "default_branch": default, "permissions": {"push": push}}
        )
    )
