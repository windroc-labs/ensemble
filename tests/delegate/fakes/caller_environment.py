"""Capture caller environments in a fresh interpreter."""

import contextlib
import json
import os
import sys
from dataclasses import replace
from unittest import mock

from tests.delegate.fakes.harness_session import harness_launches
from tests.delegate.harness import HarnessCase


def main():
    home, installed = sys.argv[1:]
    os.environ.pop("PYTHONPATH", None)
    case = HarnessCase()
    case.home = home
    case.ensemble = os.path.join(home, ".ensemble")
    os.makedirs(case.ensemble, exist_ok=True)
    case.setUp()
    layout = (
        mock.patch("sysconfig.get_paths", return_value={"purelib": installed, "platlib": installed})
        if installed
        else contextlib.nullcontext()
    )
    with layout:
        delegate = case.make(host="agent")
        delegate.detach()
        (launch,) = harness_launches()
        harness_env = {key: value for key, value in launch["env"].items() if key == "PYTHONPATH"}
        composer = delegate.composer
        base = composer.resolve("researcher", "light-research")
        access = {}
        for label, commands in (
            ("commands", ["command"]),
            ("no commands", []),
        ):
            intent = composer.access(
                replace(base, commands=commands), case.work, cwd=case.work, project=False
            )
            access[label] = {"env": dict(intent.env), "commands": list(intent.commands)}
    print(json.dumps({"harness": harness_env, "composer": access}))


if __name__ == "__main__":
    main()
