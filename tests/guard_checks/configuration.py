"""Configuration loading, level and key checks."""

import ast
import os
import re
import tomllib

from tests.guard_checks.common import _bindings, _dotted, _files
from tests.guard_data.orchestrator import (
    CONFIG_PY,
    ENTRIES,
    LOAD_HELPERS,
    ONCE,
    ORCH_PROJECT,
    ORCH_SRC,
)
from tests.guards import PKG, PROJECTS

LOCAL_EXAMPLE = re.compile(r"\s*# local: ?(.*)")


def config_once(repo: str) -> list[str]:
    found = set()
    for rel, text in _files(os.path.join(repo, "src", PKG, ORCH_PROJECT), ".py"):
        rel = f"{ORCH_SRC}{rel}"
        tree = ast.parse(text, rel)
        bound = _bindings(tree)
        mains = {
            id(n)
            for top in tree.body
            if rel in ENTRIES and isinstance(top, ast.FunctionDef) and top.name == "main"
            for n in ast.walk(top)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                called = _dotted(node.func, bound)
                if called in ONCE and id(node) not in mains:
                    found.add(f"{rel}:{node.lineno}: calls {called} outside an entry main")
                elif called.rpartition(".")[2] in LOAD_HELPERS and rel != CONFIG_PY:
                    found.add(f"{rel}:{node.lineno}: calls {called}")
    return sorted(found)


def explicit_keys(repo: str) -> list[str]:
    real, examples, found = set(), {}, set()
    for rel in (f"src/{PKG}/{package}/config.toml" for project, package in PROJECTS.items()):
        path = os.path.join(repo, rel)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as f:
            text = f.read()
        lines = [m[1] for line in text.splitlines() if (m := LOCAL_EXAMPLE.fullmatch(line))]
        try:
            real.update(_toml_leaves(tomllib.loads(text), ()))
            examples[rel] = _toml_leaves(tomllib.loads("\n".join(lines)), ())
        except tomllib.TOMLDecodeError as e:
            found.add(f"{rel}: {e}")
    keys = {k for k, empty in real}
    empty = {k for k, is_empty in real if is_empty}
    shapes = {(k[0], len(k), k[-1]) for k in keys}
    for rel, leaves in examples.items():
        for key, _ in leaves:
            k = key[1:] if key[0] in PROJECTS.values() else key
            if not k:
                continue
            if not (
                k in keys
                or (k[0], len(k), k[-1]) in shapes
                or any(k[i:] in keys for i in range(1, len(k)))
                or k[:-1] in empty
            ):
                found.add(f"{rel}: `# local:` key {'.'.join(key)} has no real line")
    return sorted(found)


def _toml_leaves(table, at):
    """(dotted key tuple, is an empty table) of each leaf."""
    out = [(at, True)] if not table and at else []
    for k, v in table.items():
        out += _toml_leaves(v, (*at, k)) if isinstance(v, dict) else [((*at, k), False)]
    return out
