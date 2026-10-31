"""Shared data type and option set checks."""

import ast
import builtins
import importlib
import re
import sys

from tests.guard_checks.common import (
    _bindings,
    _dotted,
    _private,
    _resolve_boundary,
    _source_files,
    _source_modules,
    _tree_files,
)
from tests.guard_data import OPTION_TUPLES_ALLOWED, STANDALONE
from tests.guard_data.orchestrator import ORCH_PROJECT, ORCH_SRC
from tests.guards import PROJECTS

OPTION_NAME = re.compile(r"_?[A-Z][A-Z0-9_]*")


OPTION_WORD = re.compile(r"[a-z0-9]+(?:[_-][a-z0-9]+)*")


def shared_types(repo: str) -> list[str]:
    found = []
    for project in PROJECTS:
        trees = {
            rel[:-3].replace("/", ".").removesuffix(".__init__"): (rel, ast.parse(text, rel))
            for rel, text in _source_files(repo, project, ".py")
            if f"src/{rel}" not in STANDALONE
        }
        data = {
            f"{module}.{node.name}": rel.endswith("__init__.py")
            for module, (rel, tree) in trees.items()
            for bound in (_bindings(tree),)
            for node in tree.body
            if isinstance(node, ast.ClassDef) and _data_type(node, bound)
        }
        for module, (rel, tree) in trees.items():
            if not rel.endswith("__init__.py"):
                found += _type_uses(rel, module, tree, data)
    return sorted(found)


def _data_type(node, bound):
    return any(
        _dotted(decorator.func if isinstance(decorator, ast.Call) else decorator, bound)
        == "dataclasses.dataclass"
        for decorator in node.decorator_list
    ) or any(
        _dotted(base, bound)
        in {"typing.NamedTuple", "enum.Enum", "enum.IntEnum", "enum.StrEnum", "typing.TypedDict"}
        for base in node.bases
    )


def types_only(repo: str) -> list[str]:
    found, private = [], {}
    for project in PROJECTS:
        for rel, text in _source_files(repo, project, ".py"):
            if not (rel.endswith("/types.py") or "/types/" in rel):
                continue
            tree = ast.parse(text, rel)
            bound = _bindings(tree)
            kinds = _type_kinds(tree, bound)
            for node in _module_nodes(tree):
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    if _private(node.name):
                        module = rel.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
                        private[f"{module}.{node.name}"] = f"src/{rel}"
                    else:
                        found.append(f"src/{rel}:{node.lineno}: types module function {node.name}")
                elif isinstance(node, ast.ClassDef):
                    kind = kinds.get(node.name)
                    if kind is None:
                        found.append(f"src/{rel}:{node.lineno}: {node.name}: not a type class")
                    else:
                        found += _type_methods(project, rel, node, kind, bound)
    return sorted([*found, *_types_private_uses(repo, private)])


def _types_private_uses(repo, private):
    found = set()
    modules = _source_modules(repo, PROJECTS)
    for rel, text in _tree_files(repo, ".py"):
        tree = ast.parse(text, rel)
        bound = _bindings(tree)
        uses = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                uses.extend((node.lineno, f"{node.module}.{a.name}") for a in node.names)
            elif isinstance(node, ast.Name | ast.Attribute):
                uses.append((node.lineno, _dotted(node, bound)))
        for line, name in uses:
            name = _resolve_boundary(name, modules)
            if name in private and rel != private[name]:
                found.add(f"{rel}:{line}: private types helper {name} used outside its module")
    return found


def _module_nodes(tree):
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            yield node
        else:
            yield from _module_nodes(node)


def _type_kinds(tree, bound):
    classes = [node for node in _module_nodes(tree) if isinstance(node, ast.ClassDef)]
    kinds = {}
    bases = {
        "typing.NamedTuple": "data",
        "typing.TypedDict": "data",
        "enum.Enum": "data",
        "enum.IntEnum": "data",
        "enum.StrEnum": "data",
        "typing.Protocol": "protocol",
        "abc.ABC": "abstract",
    }
    for _ in classes:
        for node in classes:
            if any(
                _dotted(d.func if isinstance(d, ast.Call) else d, bound) == "dataclasses.dataclass"
                for d in node.decorator_list
            ):
                kinds[node.name] = "dataclass"
            else:
                for base in node.bases:
                    name = _dotted(base, bound)
                    kind = bases.get(name) or kinds.get(name)
                    if _exception_type(name):
                        kind = "exception"
                    if kind:
                        kinds[node.name] = kind
                        break
    return kinds


def _exception_type(name):
    module, _, attr = name.rpartition(".")
    if not module or module == "builtins":
        value = getattr(builtins, attr or name, None)
    elif module.split(".")[0] in sys.stdlib_module_names:
        value = getattr(importlib.import_module(module), attr, None)
    else:
        return False
    return isinstance(value, type) and issubclass(value, BaseException)


def _type_methods(project, rel, node, kind, bound):
    found = []
    fields = {
        child.target.id
        for child in node.body
        if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name)
    }
    methods = {
        child.name
        for child in node.body
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    for method in (
        child for child in node.body if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
    ):
        decorators = {
            _dotted(d.func if isinstance(d, ast.Call) else d, bound) for d in method.decorator_list
        }
        if (
            project == ORCH_PROJECT
            and rel == ORCH_SRC.removeprefix("src/") + "engine/types.py"
            and node.name == "Spec"
            and method.name == "to_argv"
            and kind == "dataclass"
        ):
            continue
        if (
            method.name.startswith("__")
            and method.name.endswith("__")
            or decorators & {"property", "abc.abstractmethod"}
        ):
            continue
        if kind == "protocol" and all(
            isinstance(child, ast.Pass)
            or isinstance(child, ast.Expr)
            and isinstance(child.value, ast.Constant)
            for child in method.body
        ):
            continue
        args = [*method.args.posonlyargs, *method.args.args]
        own = [
            child.attr
            for child in ast.walk(method)
            if isinstance(child, ast.Attribute)
            and isinstance(child.value, ast.Name)
            and args
            and child.value.id == args[0].arg
        ]
        if (
            kind == "dataclass"
            and own
            and set(own) <= fields | methods
            and not decorators & {"staticmethod", "classmethod"}
        ):
            continue
        if kind == "dataclass" and "classmethod" in decorators and _builds(method, args):
            continue
        found.append(
            f"src/{rel}:{method.lineno}: {node.name}.{method.name}: business method in types module"
        )
    return found


def _builds(method, args):
    """Whether a classmethod calls its own class: an alternate constructor."""
    return bool(args) and any(
        isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id == args[0].arg
        for child in ast.walk(method)
    )


def _type_uses(rel, module, tree, data):
    package, bound, used = module.split(".")[:-1], {}, []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            source = ".".join(
                [*package[: len(package) - node.level + 1], *([node.module] if node.module else [])]
                if node.level
                else [node.module]
            )
            for a in node.names:
                bound[a.asname or a.name] = f"{source}.{a.name}"
                used.append((node.lineno, f"{source}.{a.name}"))
        elif isinstance(node, ast.Import):
            for a in node.names:
                bound[a.asname or a.name.split(".")[0]] = (
                    a.name if a.asname else a.name.split(".")[0]
                )
    used += [(n.lineno, _dotted(n, bound)) for n in ast.walk(tree) if isinstance(n, ast.Attribute)]
    return sorted(
        {
            f"src/{rel}:{line}: {key}: a shared data type belongs in a types.py"
            for line, key in used
            if key in data
            and (source := key.rpartition(".")[0]) != module
            and not source.endswith(".types")
            and not (data[key] and module.rpartition(".")[0] == source)
        }
    )


def option_tuples(repo: str) -> list[str]:
    """A module-level tuple of option words in shipped code is a StrEnum, unless allowlisted."""
    found, seen = set(), set()
    for project in PROJECTS:
        for rel, text in _source_files(repo, project, ".py"):
            rel = f"src/{rel}"
            for node in _module_assignments(ast.parse(text, rel).body):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for name in (t.id for t in targets if isinstance(t, ast.Name)):
                    if not (OPTION_NAME.fullmatch(name) and _option_words(node.value)):
                        continue
                    if (rel, name) in OPTION_TUPLES_ALLOWED:
                        seen.add((rel, name))
                    else:
                        found.add(
                            f"{rel}:{node.lineno}: {name}: a fixed option set is a StrEnum (FR-49)"
                        )
    found.update(
        f"{rel}: {name}: allowlisted but not an option tuple"
        for rel, name in OPTION_TUPLES_ALLOWED.keys() - seen
    )
    return sorted(found)


def _module_assignments(body):
    for node in body:
        if isinstance(node, ast.Assign | ast.AnnAssign):
            yield node
        elif isinstance(node, ast.If | ast.Try):
            blocks = [node.body, node.orelse, *(h.body for h in getattr(node, "handlers", ()))]
            for block in [*blocks, getattr(node, "finalbody", [])]:
                yield from _module_assignments(block)


def _option_words(value):
    return (
        isinstance(value, ast.Tuple)
        and len(value.elts) >= 2
        and all(
            isinstance(e, ast.Constant)
            and isinstance(e.value, str)
            and OPTION_WORD.fullmatch(e.value)
            for e in value.elts
        )
    )
