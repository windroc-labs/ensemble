"""STATE, path normalization, containment and temp roots."""

import contextlib
import os
import subprocess
import sys
import sysconfig
import tempfile
from collections.abc import Sequence
from types import ModuleType

STATE = "~/.ensemble"
DOCKER_STATE = f"{STATE}/docker-state"


def control_root() -> str:
    """This user's dir for local sockets, short for AF_UNIX's path limit."""
    root = os.environ.get("ENSEMBLE_CONTROL_ROOT", "/tmp")  # never macOS's long $TMPDIR
    return os.path.join(root, f"eg-{os.getuid()}")


def module_roots(*packages: ModuleType) -> tuple[str, ...]:
    roots = []
    for package in packages:
        path = package.__file__
        if path is None:
            raise ValueError(f"{package.__name__}: package has no source file")
        root = os.path.dirname(os.path.abspath(path))
        parts = package.__name__.split(".")
        if package.__spec__ is not None and package.__spec__.submodule_search_locations is None:
            parts = parts[:-1]
        for _part in parts:
            root = os.path.dirname(root)
        roots.append(root)
    return tuple(dict.fromkeys(roots))


def import_env(*packages: ModuleType) -> dict[str, str]:
    """Installed packages use the interpreter's paths; source runs supply only their roots."""
    roots = module_roots(*packages)
    if all(os.path.realpath(root) in _installed() for root in roots):
        return {}
    return {"PYTHONPATH": os.pathsep.join(roots)}


def source_path(roots: Sequence[str]) -> dict[str, str]:
    """A child's PYTHONPATH for `roots`: each once, in order, minus the interpreter's installed
    paths (as import_env); {} when none remain.
    """
    installed = _installed()
    kept = [root for root in dict.fromkeys(roots) if os.path.realpath(root) not in installed]
    return {"PYTHONPATH": os.pathsep.join(kept)} if kept else {}


def _installed() -> set[str]:
    paths = sysconfig.get_paths()
    installed = {os.path.realpath(paths[name]) for name in ("purelib", "platlib")}
    return installed & {os.path.realpath(path) for path in sys.path if path}


def overlaps(a: str, b: str) -> bool:
    """Real paths overlap when equal or either contains the other, including missing suffixes.

    PermissionError from stat propagates so inaccessible paths fail closed.
    """
    paths = tuple(os.path.realpath(os.path.expanduser(p)) for p in (a, b))
    identities = tuple(_path_identity(p) for p in paths)
    for path, other in zip(paths, reversed(identities), strict=True):
        while True:
            if _path_identity(path) == other:
                return True
            parent = os.path.dirname(path)
            if parent == path:
                break
            path = parent
    return False


def _path_identity(path: str) -> tuple[int, int, tuple[str, ...]]:
    missing = []
    while True:
        try:
            info = os.stat(path)
            # Darwin's _PC_CASE_SENSITIVE is absent from Python's pathconf_names.
            if missing and sys.platform == "darwin" and os.pathconf(path, 11) == 0:
                missing = [part.casefold() for part in missing]
            return info.st_dev, info.st_ino, tuple(reversed(missing))
        except (FileNotFoundError, NotADirectoryError):
            missing.append(os.path.basename(path))
            parent = os.path.dirname(path)
            if parent == path:
                raise
            path = parent


def real_dirs(paths, label: str) -> frozenset[str]:
    if not isinstance(paths, list):
        raise ValueError(f"{label}: want a list of local paths")
    for p in paths:
        if (
            not isinstance(p, str)
            or not p.startswith(("/", "~"))
            or os.path.expanduser(p).startswith("~")
        ):
            raise ValueError(f"{label}: {p!r:.80} is not an absolute or expandable ~ path")
    return frozenset(os.path.realpath(os.path.expanduser(p)) for p in paths)


def temp_dirs() -> tuple[str, ...]:
    dirs = [os.environ.get("TMPDIR", ""), tempfile.gettempdir(), "/tmp", "/var/tmp"]
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        dirs.append(
            subprocess.run(
                ["getconf", "DARWIN_USER_TEMP_DIR"],
                capture_output=True,
                text=True,
                timeout=60,
                stdin=subprocess.DEVNULL,
            ).stdout.strip()
        )
    return tuple(dict.fromkeys(os.path.realpath(d) for d in dirs if d))


def under(path: str, roots) -> str | None:
    return next(
        (r for r in map(os.path.realpath, roots) if os.path.commonpath([path, r]) == r), None
    )


def printable_abs(value) -> bool:
    return isinstance(value, str) and value.isprintable() and os.path.isabs(value)
