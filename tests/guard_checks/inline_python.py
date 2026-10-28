"""Python -c programs are literal text keeping the public import boundary of command callers."""

import ast
import copy
import hashlib
import os
import re
import sys

from tests.guard_checks.common import (
    _bindings,
    _dotted,
    _private,
    _resolve_boundary,
    _source_modules,
    _tree_files,
)
from tests.guard_data import PYTHON_CODE_ALLOWED
from tests.guards import PROJECTS

IMPORTERS = {"__import__", "builtins.__import__", "importlib.import_module", "runpy.run_module"}
ATTRIBUTE_CALLS = {"getattr", "setattr", "delattr", "hasattr"}
RUNTIME = object()


def inline_python(repo: str, *, allowed=None) -> list[str]:
    allowed = PYTHON_CODE_ALLOWED if allowed is None else allowed
    found, used = [], set()
    modules = _source_modules(repo, PROJECTS)
    for path, text in _tree_files(repo, ".py"):
        for line, code, error in _programs(ast.parse(text)):
            errors = [error] if error else _violations(code, modules=modules)
            if not errors:
                continue
            key = None if code is None else (path, hashlib.sha256(code.encode()).hexdigest())
            if key in allowed:
                used.add(key)
            else:
                found.append(f"{path}:{line}: Python -c: {', '.join(errors)}")
    found.extend(
        f"{path}: stale Python -c exception {digest}" for path, digest in allowed.keys() - used
    )
    found.extend(
        f"{path}: Python -c exception needs one-line reason"
        for (path, _), reason in allowed.items()
        if not isinstance(reason, str) or not reason.strip() or "\n" in reason
    )
    return sorted(set(found))


def _scope(nodes):
    for node in nodes:
        yield node
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            yield from _scope(ast.iter_child_nodes(node))


def _scope_values(tree):
    body = tree.body if isinstance(tree.body, list) else [tree.body]
    nodes = list(_scope(body))
    assigned = {}
    for node in nodes:
        if isinstance(node, ast.Assign | ast.AnnAssign):
            for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                assigned[target] = node.value
    function = isinstance(tree, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda)
    local = {}
    for node in [*(ast.walk(tree.args) if function else ()), *nodes]:
        if isinstance(node, ast.arg):
            local.setdefault(node.arg, []).append(RUNTIME)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
            value = assigned.get(node)
            local.setdefault(node.id, []).append(RUNTIME if value is None else value)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            for alias in node.names:
                local.setdefault(alias.asname or alias.name.split(".")[0], []).append(RUNTIME)
        elif (
            isinstance(
                node,
                ast.FunctionDef
                | ast.AsyncFunctionDef
                | ast.ClassDef
                | ast.ExceptHandler
                | ast.MatchAs
                | ast.MatchStar,
            )
            and node.name
        ):
            local.setdefault(node.name, []).append(RUNTIME)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            local.setdefault(node.rest, []).append(RUNTIME)
    return nodes, local


def _invalidated(tree):
    invalid = {}

    def visit(scope, parents):
        nodes, bindings = _scope_values(scope)
        globals_ = {name for node in nodes if isinstance(node, ast.Global) for name in node.names}
        nonlocals = {
            name for node in nodes if isinstance(node, ast.Nonlocal) for name in node.names
        }
        declared = globals_ | nonlocals
        local = bindings.keys() - declared

        def owner(name):
            if name in globals_:
                return tree
            if name in local:
                return scope
            for outer, names in reversed(parents):
                if not isinstance(outer, ast.ClassDef) and name in names:
                    return outer
            return tree

        for name in bindings.keys() & declared:
            invalid.setdefault(id(owner(name)), set()).add(name)
        for node in nodes:
            if isinstance(node, ast.Attribute | ast.Subscript) and isinstance(
                node.ctx, ast.Store | ast.Del
            ):
                root = node.value
                while isinstance(root, ast.Attribute | ast.Subscript):
                    root = root.value
                if isinstance(root, ast.Name):
                    invalid.setdefault(id(owner(root.id)), set()).add(root.id)
            elif isinstance(
                node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda
            ):
                visit(node, [*parents, (scope, local)])

    visit(tree, [])
    return invalid


def _programs(tree, inherited=None, invalid=None):
    """(line, text, error) per -c program; runtime bindings cannot have code exceptions."""
    invalid = _invalidated(tree) if invalid is None else invalid
    nodes, local = _scope_values(tree)
    declarations = {
        name for node in nodes if isinstance(node, ast.Global | ast.Nonlocal) for name in node.names
    }
    local = {name: values for name, values in local.items() if name not in declarations}
    for name in invalid.get(id(tree), ()):
        local.setdefault(name, []).append(RUNTIME)
    bindings = {**(inherited or {}), **local}
    for node in nodes:
        if isinstance(node, ast.List | ast.Tuple) and node.elts and _python(node.elts[0], bindings):
            for before, after in zip(node.elts, node.elts[1:], strict=False):
                if _strings(before, bindings) != {"-c"}:
                    continue
                if codes := _strings(after, bindings):
                    yield from ((after.lineno, code, None) for code in sorted(codes))
                    continue
                text, runtime = _substitute(after, bindings)
                hint = " (run-time value; build the argv from literals at the call site)"
                yield (
                    after.lineno,
                    None if runtime else text,
                    f"not a literal: {ast.unparse(after)}{hint if runtime else ''}",
                )
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            # Class bindings are not visible inside its methods.
            yield from _programs(
                node, inherited if isinstance(tree, ast.ClassDef) else bindings, invalid
            )


def _strings(node, bindings, seen=frozenset()):
    """Every text node can take, or the empty set when any of them is not literal."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.Name) and node.id not in seen and bindings.get(node.id):
        values = [
            _strings(value, bindings, seen | {node.id}) if isinstance(value, ast.AST) else set()
            for value in bindings[node.id]
        ]
        return set().union(*values) if all(values) else set()
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return {
            left + right
            for left in _strings(node.left, bindings, seen)
            for right in _strings(node.right, bindings, seen)
        }
    return set()


def _substitute(node, bindings):
    runtime = False

    class Substitute(ast.NodeTransformer):
        seen = set()

        def visit_Name(self, name):
            nonlocal runtime
            values = bindings.get(name.id, ())
            if len(values) != 1 or values[0] is RUNTIME or name.id in self.seen:
                runtime = True
                return name
            self.seen.add(name.id)
            value = self.visit(copy.deepcopy(values[0]))
            self.seen.discard(name.id)
            return value

    return ast.unparse(Substitute().visit(copy.deepcopy(node))), runtime


def _python(node, bindings, seen=frozenset()):
    if isinstance(node, ast.Starred):
        return _python(node.value, bindings, seen)
    if isinstance(node, ast.Name) and node.id not in seen:
        if node.id in {"PY", "PYTHON", "PYTHON3", "python"}:
            return True
        return any(
            _python(value, bindings, seen | {node.id})
            for value in bindings.get(node.id, ())
            if isinstance(value, ast.AST)
        )
    if isinstance(node, ast.List | ast.Tuple) and node.elts:
        return _python(node.elts[0], bindings, seen)
    if any(_dotted(part, {}) == "sys.executable" for part in ast.walk(node)):
        return True
    return any(
        re.fullmatch(r"python(?:3(?:\.\d+)?)?", os.path.basename(word))
        for word in _strings(node, bindings)
    )


def _public(module):
    parts = module.split(".")
    return (
        parts[0] in sys.stdlib_module_names
        or len(parts) == 2
        and parts[0] == "ensemble"
        and parts[1] in PROJECTS.values()
    )


def _command(module):
    parts = module.split(".")
    return (
        len(parts) == 3
        and parts[0] == "ensemble"
        and parts[1] in PROJECTS.values()
        and parts[2] == "cli"
    )


def _stdlib_names(tree):
    """Names bound only by imports of standard library modules."""
    bound = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                bound.setdefault(alias.asname or top, []).append(top in sys.stdlib_module_names)
        elif isinstance(node, ast.ImportFrom):
            stdlib = not node.level and (node.module or "").split(".")[0] in sys.stdlib_module_names
            for alias in node.names:
                bound.setdefault(alias.asname or alias.name, []).append(stdlib)
        elif isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            bound.setdefault(node.id, []).append(False)
        elif isinstance(node, ast.arg):
            bound.setdefault(node.arg, []).append(False)
        elif (
            isinstance(
                node, ast.ExceptHandler | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
            )
            and node.name
        ):
            bound.setdefault(node.name, []).append(False)
    return {name for name, stdlib in bound.items() if all(stdlib)}


def _violations(code, *, modules):
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError) as error:
        return [f"invalid code: {error}"]
    bound = _bindings(tree)
    found, agent = set(), False
    for node in ast.walk(tree):
        imports = []
        if isinstance(node, ast.Import):
            imports = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imports = ["." * node.level + (node.module or "")]
            if (node.module or "").startswith("ensemble."):
                found.update(
                    f"private import {alias.name}" for alias in node.names if _private(alias.name)
                )
                found.update(
                    f"import {node.module}.{alias.name}"
                    for alias in node.names
                    if _resolve_boundary(f"{node.module}.{alias.name}", modules) in modules
                )
        elif isinstance(node, ast.Call) and (call := _dotted(node.func, bound)) in {
            *IMPORTERS,
            "runpy.run_path",
        }:
            names = _strings(node.args[0], {}) if node.args and call in IMPORTERS else set()
            if not names:
                found.add(f"dynamic import: {ast.unparse(node)}")
            imports = [
                name for name in names if not (call == "runpy.run_module" and _command(name))
            ]
            agent = agent or any(name.split(".")[0] == "ensemble" for name in names)
        for module in imports:
            agent = agent or module.split(".")[0] == "ensemble"
            if module.startswith(".") or not _public(module):
                found.add(f"import {module}")
    if agent:
        stdlib = _stdlib_names(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and _private(node.attr):
                base, names = node.value, {node.attr}
            elif (
                isinstance(node, ast.Call)
                and _dotted(node.func, bound) in ATTRIBUTE_CALLS
                and len(node.args) > 1
            ):
                base, names = node.args[0], {n for n in _strings(node.args[1], {}) if _private(n)}
            else:
                continue
            if not (isinstance(base, ast.Name) and base.id in stdlib):
                found.update(f"private attribute {name}" for name in names)
    return sorted(found)
