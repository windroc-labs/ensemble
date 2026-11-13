"""The open-run index: one file in ROOT per run that may be alive, named for its workdir's real
path and holding it; MARK once seeded with the runs from before it. OSErrors propagate.
"""

import contextlib
import hashlib
import os
import re

from ensemble.utils import STATE, save

ROOT = f"{STATE}/delegate/open"
MARK = ".seeded"
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


def mark() -> None:
    save(os.path.join(os.path.expanduser(ROOT), MARK), "", durable=True)


def marked() -> bool:
    return os.path.isfile(os.path.join(os.path.expanduser(ROOT), MARK))


def listed() -> list[str]:
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
