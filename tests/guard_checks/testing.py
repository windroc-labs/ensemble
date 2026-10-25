"""Test placement and isolation checks."""

import ast
import os

from tests.guard_checks.common import (
    _boundary_bindings,
    _boundary_name,
    _resolve_boundary,
    _source_modules,
    _tree_files,
    _within,
)
from tests.guards import PKG, PROJECTS, UNIT_ISOLATION_ALLOWED


def _boundary_violations(text, own, modules=None, *, shipped=False):
    tree = ast.parse(text)
    aliases, modules = _boundary_bindings(tree), modules or {}
    found = set()
    for node in ast.walk(tree):
        if shipped:
            if _test_switch(node, aliases):
                found.add((node.lineno, "shipped test switch"))
            continue
        if isinstance(node, ast.Import | ast.ImportFrom):
            names = [a.name for a in node.names] + [getattr(node, "module", "") or ""]
            if any(_test_module(name) for name in names):
                found.add((node.lineno, "test module import"))
        name = _resolve_boundary(_boundary_name(node, aliases), modules)
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Store | ast.Del)
            and _within(name, f"ensemble.{own}")
        ):
            found.add((node.lineno, "own project attribute mutation"))
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Store | ast.Del)
            and _boundary_name(node.value, aliases) == "sys.modules"
        ) or (
            isinstance(node, ast.Attribute)
            and _boundary_name(node.value, aliases) == "sys.modules"
            and node.attr
            in {"pop", "popitem", "update", "clear", "setdefault", "__setitem__", "__delitem__"}
        ):
            found.add((node.lineno, "sys.modules mutation"))
        if isinstance(node, ast.Call):
            found.update(
                (node.lineno, reason) for reason in _boundary_calls(node, aliases, own, modules)
            )
    return sorted(found)


def _test_module(name):
    return any(part.endswith("_test") for part in name.split("."))


def _test_switch(node, aliases):
    candidates = []
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
        candidates.append(node.id)
    if (
        isinstance(node, ast.Call)
        and _boundary_name(node.func, aliases) in {"os.getenv", "os.environ.get"}
        and node.args
    ):
        candidates.append(_boundary_name(node.args[0], aliases))
    if isinstance(node, ast.Subscript) and _boundary_name(node.value, aliases) == "os.environ":
        candidates.append(_boundary_name(node.slice, aliases))
    return any(name.endswith("_TEST") or name.startswith("FORCE_") for name in candidates)


def _boundary_calls(node, aliases, own, modules):
    arguments = node.args or [k.value for k in node.keywords if k.arg in {"target", "in_dict"}]
    if not arguments:
        return []
    call, raw = _boundary_name(node.func, aliases), _boundary_name(arguments[0], aliases)
    target = _resolve_boundary(raw, modules)
    rules = {
        "shipped code patch": call.endswith((".patch", ".patch.multiple"))
        and (_within(raw, "ensemble") or _within(target, "ensemble")),
        "shipped object patch": call.endswith(".patch.object") and _within(target, "ensemble"),
        "sys.modules dictionary patch": call.endswith(".patch.dict") and target == "sys.modules",
        "shipped dictionary patch": call.endswith(".patch.dict") and _within(target, "ensemble"),
        "own project attribute mutation": call in {"setattr", "delattr"}
        and _within(target, f"ensemble.{own}"),
        "test module import": (call.endswith("import_module") or call == "__import__")
        and _test_module(raw),
    }
    return [reason for reason, violated in rules.items() if violated]


def unit_test_modules(repo: str) -> list[str]:
    """Unit <rel>/<name>_test.py matches a source module or nonempty directory at the same path, or
    a helper module under the project's tests. Package tests use the package name, including the
    layer package at unit root. Integration tests and non-_test.py helpers are outside this rule.
    """
    found = []
    for project, layer in PROJECTS.items():
        tests = os.path.join(repo, "tests", project)
        for top, dirs, names in os.walk(tests):
            parts = os.path.relpath(top, tests).split(os.sep)
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            if "unit" not in parts:
                dirs[:] = [d for d in dirs if d != "integration"]
                continue
            unit = parts.index("unit")
            relative = parts[unit + 1 :]
            for name in sorted(n for n in names if n.endswith("_test.py")):
                stem = name.removesuffix("_test.py")
                source = os.path.join("src", PKG, layer, *relative, stem)
                home = os.path.join(repo, source)
                helper = os.path.join(tests, *parts[:unit], *relative, stem + ".py")
                matches = (
                    os.path.isfile(home + ".py")
                    or os.path.isfile(os.path.join(home, "__init__.py"))
                    or any(files for _, _, files in os.walk(home))
                    or os.path.isfile(helper)
                    or (
                        not relative
                        and stem == layer
                        and os.path.isfile(os.path.join(repo, "src", PKG, layer, "__init__.py"))
                    )
                )
                if not stem or stem == "__init__" or not matches:
                    test = os.path.relpath(os.path.join(top, name), repo)
                    found.append(f"{test}: missing source module {source}.py")
    return sorted(found)


def unit_isolation_allowlist(repo: str) -> list[str]:
    return (
        [f"{os.path.join(repo, 'tests/guards.py')}: unit isolation allowlist must remain empty"]
        if UNIT_ISOLATION_ALLOWED
        else []
    )


def test_gate_mutation(repo: str) -> list[str]:
    found = set()
    for rel, text in _tree_files(repo, ".py"):
        if "tests" not in rel.split("/") and not (
            rel.startswith(".github/") and rel.endswith("_test.py")
        ):
            continue
        tree = ast.parse(text)
        aliases = _boundary_bindings(tree)
        for node in ast.walk(tree):
            name = _boundary_name(node, aliases)
            if isinstance(node, ast.Name | ast.Attribute) and any(
                _within(name, target) for target in ("run_tests.GATE", "run_tests.isolation")
            ):
                found.add(f"{rel}:{node.lineno}: interpreter gate access")
            if not isinstance(node, ast.Call) or not node.args:
                continue
            call = _boundary_name(node.func, aliases)
            target = _boundary_name(node.args[0], aliases)
            if ("patch" in call.split(".") or call in {"setattr", "delattr"}) and any(
                _within(target, name)
                for name in ("isolation", "run_tests.GATE", "run_tests.isolation")
            ):
                found.add(f"{rel}:{node.lineno}: interpreter gate patch")
    return sorted(found)


def test_boundaries(repo: str) -> list[str]:
    modules = _source_modules(repo, PROJECTS)
    return sorted(
        f"{rel}:{line}: {reason}"
        for rel, text in _tree_files(repo, ".py")
        for line, reason in _boundary_violations(
            text,
            rel.split("/")[2] if rel.startswith("src/ensemble/") else rel.split("/")[1],
            modules,
            shipped=rel.startswith("src/"),
        )
    )
