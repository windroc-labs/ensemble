"""Client unit helpers: argv runs and the packaged [clients.<name>] tables."""

import contextlib
import os
from dataclasses import replace
from unittest import mock

from ensemble.session.clients import registry
from ensemble.session.engine.client_state import LocalHome
from ensemble.session.engine.config import read_config
from ensemble.session.engine.types import LOCAL_CONTEXT, HomeStore, Permission


def holds(argv, words) -> bool:
    return any(tuple(argv[i : i + len(words)]) == tuple(words) for i in range(len(argv)))


def packaged_table(client: str, local: str = os.devnull) -> dict:
    """The packaged session config.toml's [clients.<client>], under `local`, without
    implementation and max_runs."""
    table = dict(registry.settings(read_config(local=local))["clients"][client])
    table.pop("implementation")
    table.pop("max_runs")
    return table


def homed(run, home: HomeStore | None = None, permission=Permission.AUTO):
    """`run` given a local context holding `home`, this machine's by default, as a mode gives it."""
    run._context = replace(LOCAL_CONTEXT, permission=permission, home=home or LocalHome())
    return run


@contextlib.contextmanager
def opened_below(root: str, *, itself: bool):
    """Paths under `root` (`itself`: or it) that os.open, stat, lstat or scandir get by name."""
    roots = {os.path.abspath(root), os.path.realpath(root)}
    touched: list[str] = []

    def watched(real):
        def call(path=os.curdir, *args, **kwargs):
            if kwargs.get("dir_fd") is None and isinstance(path, (str, bytes, os.PathLike)):
                found = os.path.abspath(os.fsdecode(path))
                if any(found.startswith(r + os.sep) or itself and found == r for r in roots):
                    touched.append(found)
            return real(path, *args, **kwargs)

        return call

    with contextlib.ExitStack() as stack:
        for name in ("open", "stat", "lstat", "scandir"):
            stack.enter_context(mock.patch(f"os.{name}", watched(getattr(os, name))))
        yield touched
