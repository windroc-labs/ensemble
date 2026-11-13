"""The open-run index: one file in ROOT per run that may still be alive, named for its workdir's
real path and holding that path. A run adds its entry before it saves a state that can make it
alive; only a check finding the run unable to come alive again without another save removes it.
Its OSErrors propagate.
"""

import contextlib
import hashlib
import os
import re

from ensemble.utils import STATE, save

ROOT = f"{STATE}/delegate/open"
_ENTRY = re.compile(r"[0-9a-f]{32}")


def _path(workdir: str) -> str:
    digest = hashlib.sha256(os.path.realpath(workdir).encode()).hexdigest()[:32]
    return os.path.join(os.path.expanduser(ROOT), digest)


def add(workdir: str) -> None:
    path = _path(workdir)
    if not os.path.isfile(path):
        save(path, os.path.realpath(workdir), durable=True)


def remove(workdir: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(_path(workdir))


def listed() -> list[str]:
    """The workdirs of the index's entries; none before the first entry."""
    root = os.path.expanduser(ROOT)
    try:
        names = os.listdir(root)
    except FileNotFoundError:
        return []
    found = []
    for name in sorted(names):
        if _ENTRY.fullmatch(name):
            with (
                contextlib.suppress(FileNotFoundError),
                open(os.path.join(root, name), encoding="utf-8") as file,
            ):
                found.append(file.read())
    return found
