"""Run repository design checks with the same discovery as their unit tests."""

import importlib
import inspect
import os
import pkgutil
import sys
from pathlib import Path

from tests import guard_checks, guards

ROOT = Path(__file__).resolve().parents[1]

MODULES = tuple(
    importlib.import_module(f"{guard_checks.__name__}.{module.name}")
    for module in pkgutil.iter_modules(guard_checks.__path__)
)


def discover(modules=MODULES):
    """(name, check) of each public function defined in `modules`, by name; names are unique."""
    found = {}
    for module in modules:
        for name, check in inspect.getmembers(module, inspect.isfunction):
            if name.startswith("_") or check.__module__ != module.__name__:
                continue
            if name in found:
                raise ValueError(f"{name}: in {found[name].__module__} and {module.__name__}")
            found[name] = check
    return sorted(found.items())


def checks(root):
    for name, check in discover():
        roots = (
            [os.path.join(root, project, "src") for project in guards.PROJECTS]
            if name == "private_imports"
            else [root]
        )
        yield name, [violation for at in roots for violation in check(str(at))]


def main():
    failed = False
    for name, found in checks(ROOT):
        print(f"{name}: {len(found)} violations")
        if found:
            failed = True
            print("\n".join(found))
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
