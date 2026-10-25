"""Namespace, subsystem and runtime-path placement checks."""

import ast
import os

from tests.guard_checks.common import _bindings, _dotted, _files
from tests.guard_data import ENTRY_MODULES, SUBPACKAGES
from tests.guard_data.orchestrator import ORCH_ABSENT, ORCH_PROJECT, ORCH_SRC
from tests.guards import PKG, PROJECTS

LEFTOVERS = {"__pycache__", ".DS_Store"}


TOP = {"__init__.py", "cli.py", "engine"}


def orchestrator_entry(repo: str) -> list[str]:
    found = []
    root = os.path.join(repo, ORCH_PROJECT, ORCH_SRC)
    if not os.path.isfile(os.path.join(root, "cli.py")):
        found.append(f"{ORCH_PROJECT}/{ORCH_SRC}cli.py: missing entry")
    found += [
        f"{ORCH_PROJECT}/{ORCH_SRC}{rel}: no such module: a run is its Delegate's harness (FR-25)"
        for rel in ORCH_ABSENT
        if os.path.lexists(os.path.join(root, rel))
    ]
    for rel, text in _files(root, ".py"):
        if rel == "cli.py":
            continue
        for node in ast.walk(ast.parse(text, rel)):
            if isinstance(node, ast.Compare) and any(
                isinstance(value, ast.Constant) and value.value == "__main__"
                for value in (node.left, *node.comparators)
            ):
                found.append(f"{ORCH_PROJECT}/{ORCH_SRC}{rel}:{node.lineno}: entry outside cli.py")
    return sorted(found)


def package_top(repo: str) -> list[str]:
    found = set()
    for project in (p for p in PROJECTS if p != "utils"):
        rel = f"{project}/src/{PKG}/{PROJECTS[project]}"
        path = os.path.join(repo, rel)
        have = {e for e in (os.listdir(path) if os.path.isdir(path) else ()) if e not in LEFTOVERS}
        found.update(f"{rel}/{e}: missing" for e in (TOP - {"cli.py"}) - have)
        listed = (
            TOP
            | {"config.toml"}
            | SUBPACKAGES.get(project, set())
            | ENTRY_MODULES.get(project, set())
        )
        found.update(
            f"{rel}/{e}: not __init__.py, cli.py, config.toml, engine/, a listed subpackage or a listed entry"
            " module"
            for e in have - listed
        )
    return sorted(found)


def namespace_package(repo: str) -> list[str]:
    found = set()
    for top, own in PROJECTS.items():
        for rel, wanted in (("src", {PKG}), (f"src/{PKG}", {own})):
            path = os.path.join(repo, top, rel)
            have = {
                e
                for e in (os.listdir(path) if os.path.isdir(path) else ())
                if e not in LEFTOVERS and not e.endswith(".egg-info")
            }
            found.update(
                f"{top}/{rel}/{e}: "
                + ("namespace package __init__.py" if e == "__init__.py" else f"not {top}'s")
                for e in have - wanted
            )
            found.update(f"{top}/{rel}/{e}: missing" for e in wanted - have)
    return sorted(found)


def package_climbs(repo: str) -> list[str]:
    """Runtime paths stay inside their own package."""
    found = []
    for project, package in PROJECTS.items():
        base = f"{project}/src/ensemble/{package}"
        for sub, text in _files(os.path.join(repo, base), (".py",)):
            rel = f"{base}/{sub}"
            depth = len(sub.split("/"))
            tree = ast.parse(text, rel)
            bound = _bindings(tree)
            paths = dict.fromkeys(("ensemble.utils.ROOT", "ensemble.utils.paths.ROOT"), depth + 1)
            paths.update({name: depth + 1 for name, target in bound.items() if target in paths})
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module in (
                    "ensemble.utils",
                    "ensemble.utils.paths",
                ):
                    if node.module.startswith(f"ensemble.{package}."):
                        continue
                    for alias in node.names:
                        name = "import_roots()" if alias.name == "import_roots" else alias.name
                        used = any(
                            isinstance(use, ast.Name)
                            and isinstance(use.ctx, ast.Load)
                            and use.id == (alias.asname or alias.name)
                            for use in ast.walk(tree)
                        )
                        if alias.name in ("ROOT", "import_roots") and not used:
                            found.append(f"{rel}:{node.lineno}: {name}: path escapes its package")
            for name, line in _package_paths(tree, bound, paths, depth, project):
                found.append(f"{rel}:{line}: {name}: path escapes its package")
    return sorted(set(found))


def _package_paths(node, bound, paths, depth, project, *, scope=""):
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        paths = dict(paths)
        args = [*node.args.posonlyargs, *node.args.args]
        defaults = [
            *zip(args[len(args) - len(node.args.defaults) :], node.args.defaults, strict=True),
            *zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True),
        ]
        for arg, default in defaults:
            climb = _package_climb(default, bound, paths)
            if climb is not None:
                paths[arg.arg] = climb
                if climb > depth and isinstance(default, ast.Name) and default.id in bound:
                    yield node.name, default.lineno
        scope = node.name
    if isinstance(node, ast.Assign | ast.AnnAssign):
        value = node.value
        climb = _package_climb(value, bound, paths)
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and climb is not None:
                paths[target.id] = climb
                if climb > depth and _adds_climb(value, bound):
                    yield scope or target.id, node.lineno
        scope = scope or next((t.id for t in targets if isinstance(t, ast.Name)), "")
    name = _dotted(node, bound)
    if isinstance(node, ast.Name | ast.Attribute) and isinstance(node.ctx, ast.Load):
        if name in ("ensemble.utils.ROOT", "ensemble.utils.paths.ROOT"):
            yield scope or "ROOT", node.lineno
        if name in ("ensemble.utils.import_roots", "ensemble.utils.paths.import_roots"):
            yield "import_roots()", node.lineno
    if isinstance(node, ast.Call):
        name = _dotted(node.func, bound)
        if name == "os.listdir" and node.args:
            climb = _package_climb(node.args[0], bound, paths)
            if climb is not None and climb > depth:
                yield scope, node.lineno
    if isinstance(node, ast.Call | ast.BinOp):
        parts = node.args[1:] if isinstance(node, ast.Call) else [node.right]
        is_join = (
            _dotted(node.func, bound) == "os.path.join"
            if isinstance(node, ast.Call)
            else isinstance(node.op, ast.Div)
        )
        base = (
            node.args[0]
            if isinstance(node, ast.Call) and node.args
            else node.left
            if isinstance(node, ast.BinOp)
            else None
        )
        if (
            is_join
            and (climb := _package_climb(base, bound, paths)) is not None
            and climb > depth
            and any(
                isinstance(part, ast.Constant)
                and isinstance(part.value, str)
                and set(part.value.split("/")) & (PROJECTS.keys() - {project})
                for part in parts
            )
        ):
            yield scope, node.lineno
    if _adds_climb(node, bound):
        climb = _package_climb(node, bound, paths)
        if climb is not None and climb > depth:
            yield scope, node.lineno
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "parents"
        and not (
            isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, int)
            and node.slice.value >= 0
        )
        and _package_climb(node.value.value, bound, paths) is not None
    ):
        yield scope, node.lineno
    for child in ast.iter_child_nodes(node):
        yield from _package_paths(child, bound, paths, depth, project, scope=scope)


def _adds_climb(node, bound):
    return (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Div)
        and _path_steps(node.right, bound) > 0
        or isinstance(node, ast.Attribute)
        and node.attr == "parent"
        or isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "parents"
        or isinstance(node, ast.Call)
        and (
            _dotted(node.func, bound) == "os.path.dirname"
            or _dotted(node.func, bound) == "os.path.join"
            and any(_path_steps(a, bound) > 0 for a in node.args[1:])
        )
    )


def _package_climb(node, bound, paths):
    if (name := _dotted(node, bound)) in ("ensemble.utils.ROOT", "ensemble.utils.paths.ROOT"):
        return paths.get(name)
    if isinstance(node, ast.Name):
        return 0 if node.id == "__file__" else paths.get(node.id)
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        base = _package_climb(node.value, bound, paths)
        return None if base is None else base + 1
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "parents"
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, int)
    ):
        base = _package_climb(node.value.value, bound, paths)
        return None if base is None else base + node.slice.value + 1
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        base = _package_climb(node.left, bound, paths)
        return None if base is None else base + _path_steps(node.right, bound)
    if not isinstance(node, ast.Call):
        return None
    name = _dotted(node.func, bound)
    if name in ("os.path.dirname", "os.path.join") and node.args:
        base = _package_climb(node.args[0], bound, paths)
        return (
            None
            if base is None
            else base
            + (1 if name.endswith("dirname") else sum(_path_steps(a, bound) for a in node.args[1:]))
        )
    if name in ("pathlib.Path", "os.path.abspath", "os.path.realpath", "str") and node.args:
        return _package_climb(node.args[0], bound, paths)
    if isinstance(node.func, ast.Attribute) and node.func.attr in ("resolve", "absolute"):
        return _package_climb(node.func.value, bound, paths)
    return None


def _path_steps(node, bound):
    if isinstance(node, ast.Starred):
        return _path_steps(node.value, bound)
    if isinstance(node, ast.List | ast.Tuple):
        return sum(_path_steps(v, bound) for v in node.elts)
    if (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Mult)
        and isinstance(node.right, ast.Constant)
        and isinstance(node.right.value, int)
    ):
        return _path_steps(node.left, bound) * node.right.value
    if _dotted(node, bound) == "os.pardir":
        return 1
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return sum(
            1 if part == ".." else -1 for part in node.value.split("/") if part not in ("", ".")
        )
    return 0
