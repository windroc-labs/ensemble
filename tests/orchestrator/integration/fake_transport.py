"""Loopback HTTP transport, fake credential process and no worker Linear access for integration
subprocesses.
"""

import contextlib
import json
import os
import shlex
import subprocess
import sys
import urllib.request
from unittest import mock

from ensemble.orchestrator.trackers.linear import worker

ENV = "FAKE_LINEAR_TRANSPORT"
SPAWNS = {"subprocess.Popen", "os.exec", "os.posix_spawn"}


def _name(path):
    return (
        os.path.basename(os.fsdecode(path)) if isinstance(path, str | bytes | os.PathLike) else ""
    )


def refuse_keychain(event, args):
    """Audit hook: under the fake no real `security` (the Keychain) starts, by any route: its
    executable, argv[0] or a shell's -c command. The fake answers /usr/bin/security unspawned.
    """
    if event not in SPAWNS:
        return
    executable, argv = args[0], args[1]
    argv = [argv] if isinstance(argv, str | bytes | os.PathLike) else list(argv or ())
    first = argv[0] if argv else None
    names = {_name(executable), _name(first)}
    if "-c" in argv[1:-1]:
        command = argv[argv.index("-c") + 1]
        names.add(_name((shlex.split(os.fsdecode(command)) or [""])[0]))
    if "security" in names:
        message = f"fake_transport.py: refused a real Keychain call: {argv}"
        print(message, file=sys.stderr)
        raise RuntimeError(message)


def install(path):
    run = subprocess.run
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def settings():
        with open(os.environ.get(ENV, path), encoding="utf-8") as stream:
            value = json.load(stream)
        url = urllib.parse.urlsplit(value["url"])
        if url.scheme != "http" or url.hostname not in ("127.0.0.1", "::1") or url.port is None:
            raise ValueError("fake transport requires a loopback HTTP endpoint")
        return value

    def send(request, *, timeout):
        if request.full_url != "https://api.linear.app/graphql":
            raise ValueError("fake transport refuses an unexpected endpoint")
        value = settings()
        redirected = urllib.request.Request(
            value["url"], data=request.data, headers=dict(request.header_items())
        )
        return opener.open(redirected, timeout=timeout)

    def process(argv, *args, **kwargs):
        if not argv or argv[0] != "/usr/bin/security":
            return run(argv, *args, **kwargs)
        if (
            len(argv) not in (4, 5)
            or list(argv[1:3]) != ["find-generic-password", "-s"]
            or (len(argv) == 5 and argv[4] != "-w")
        ):
            raise ValueError("fake transport refuses an unexpected credential command")
        token = settings()["tokens"].get(argv[3])
        result = subprocess.CompletedProcess(
            argv,
            0 if token is not None else 44,
            (token + "\n") if token is not None and len(argv) == 5 else "",
            "",
        )
        if kwargs.get("check") and result.returncode:
            raise subprocess.CalledProcessError(result.returncode, argv)
        return result

    stack = contextlib.ExitStack()
    stack.enter_context(mock.patch.object(urllib.request, "urlopen", send))
    stack.enter_context(mock.patch.object(subprocess, "run", process))
    worker_access = os.path.join(os.path.dirname(os.path.abspath(path)), "worker", "linear")
    stack.enter_context(mock.patch.object(worker, "DIRECTORY", worker_access))
    return stack
