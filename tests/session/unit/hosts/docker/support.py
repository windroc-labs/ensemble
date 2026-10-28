"""The broker protocol's shared fixtures and a fake broker runner."""

import json
import os
from types import SimpleNamespace

from tests.session.case import PLUGIN_BIN, REPO

FIXTURES = os.path.join(REPO, "src", "broker", "protocol", "docker-broker", "fixtures")
HOOK = os.path.join(PLUGIN_BIN, "ensemble-session")
UPDRAFT = (
    "updraft@windroc-labs/updraft#33f01c3b9e398e009f1fb1a4554ddcf19209dd4e"
    "::windroc-labs/agent-plugins-marketplace#dbea3408ab0c113bd74f414ab3a19d820e0f6e43"
)
BETA = "beta@owner/market#" + "b" * 40


def cases():
    """Every table's cases, with their definition."""
    found = []
    for table in sorted(os.listdir(FIXTURES)):
        with open(os.path.join(FIXTURES, table)) as f:
            found += [{**c, "definition": table.removesuffix(".json")} for c in json.load(f)]
    return found


def data(name):
    return {c["name"]: c["data"] for c in cases()}[name]


def text(name):
    return json.dumps(data(name))


class FakeBroker:
    """Plays the broker: each call pops the next (rc, stdout, stderr) or exception; records argv and
    keywords.
    """

    def __init__(self, *results):
        self.results, self.calls, self.kwargs = list(results), [], []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        self.kwargs.append(kw)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        rc, out, err = result
        return SimpleNamespace(args=argv, returncode=rc, stdout=out, stderr=err)


def success(name):
    return 0, text(name), ""


def failure(name, rc=None):
    found = data(name)
    code = found.get("exit_code")
    return (code if rc is None else rc), "", text(name)


class FakeProc:
    """A Popen result whose stdout and stderr are pipes holding `out` and `err`, closed unless
    `hang`.
    """

    def __init__(self, argv, out=b"", err=b"", code=0, *, hang=False):
        self.args, self.code, self.killed, self.fds = argv, code, False, []
        self.stdout, self.stderr = self._pipe(out, hang=hang), self._pipe(err, hang=hang)

    def _pipe(self, data, *, hang):
        read, write = os.pipe()
        os.write(write, data)
        if hang:
            self.fds.append(write)
        else:
            os.close(write)
        return os.fdopen(read, "rb")

    def wait(self, timeout=None):
        return self.code

    def kill(self):
        self.killed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        for fd in self.fds:
            os.close(fd)
        self.stdout.close()
        self.stderr.close()


class FakePopen:
    """subprocess.Popen: each call pops the next (out, err, code) as a FakeProc; records argv,
    keywords and the stdin it was given.
    """

    def __init__(self, *results):
        self.results, self.calls, self.procs = list(results), [], []

    def __call__(self, argv, **kw):
        stdin = kw.get("stdin")
        self.calls.append((argv, kw, stdin.read() if hasattr(stdin, "read") else stdin))
        self.procs.append(FakeProc(argv, *self.results.pop(0)))
        return self.procs[-1]
