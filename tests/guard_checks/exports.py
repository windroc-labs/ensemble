"""Entry-module export checks and the session's and delegate's frozen interfaces."""

import ast
import builtins
import enum
import importlib
import inspect
import json
import os
import sys
import sysconfig
import textwrap
import types

from tests.guard_checks.common import _at_import, _bindings, _dotted, _files, _resolve_boundary
from tests.guard_data import OBJECT_EXPORT_MODULES, OBJECT_EXPORTS_ALLOWED
from tests.guard_data.delegate import DELEGATE_INTERFACE, DELEGATE_INTERFACE_MODULES
from tests.guard_data.orchestrator import ORCH_PROJECT
from tests.guard_data.session import SESSION_INTERFACE, SESSION_INTERFACE_MODULES
from tests.guards import PROJECTS


def manager_exports(repo: str) -> list[str]:
    base = "manager/src/ensemble/manager/__init__.py"
    path = os.path.join(repo, base)
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as source:
        exports = _all_node(ast.parse(source.read(), base))
    if exports is None:
        return [f"{base}: missing __all__"]
    try:
        names = ast.literal_eval(exports)
    except (ValueError, TypeError):
        return [f"{base}:{exports.lineno}: __all__ must be a literal sequence of names"]
    if not isinstance(names, list | tuple) or not all(isinstance(name, str) for name in names):
        return [f"{base}:{exports.lineno}: __all__ must be a literal sequence of names"]
    found = []
    for name, (_, kind) in _export_kinds(repo, "ensemble.manager", names).items():
        if kind == "function":
            found.append(f"{base}:{exports.lineno}: exports function {name}")
        elif kind not in ("class", "constant") and (not name.isupper() or kind == "module"):
            found.append(f"{base}:{exports.lineno}: exports nonconstant {name}")
    return sorted(found)


def object_exports(repo: str) -> list[str]:
    """FR-44: entry modules export only classes, constants and type aliases unless allowlisted."""
    found, seen = set(), set()
    for rel in OBJECT_EXPORT_MODULES:
        path = os.path.join(repo, rel)
        project = rel.split("/", 1)[0]
        if not os.path.isfile(path):
            if project in ("delegate", ORCH_PROJECT):
                found.add(f"{rel}: no literal __all__")
            continue
        package = rel.removeprefix(f"{project}/src/").removesuffix(".py").replace("/", ".")
        package = package.removesuffix(".__init__")
        with open(path, encoding="utf-8") as source:
            exports = _all_node(ast.parse(source.read(), rel))
        literal = isinstance(exports, ast.List) and _is_literal(exports)
        names = ast.literal_eval(exports) if literal else None
        if names is None or not all(isinstance(name, str) for name in names):
            found.add(f"{rel}: no literal __all__")
            continue
        for name, (_, kind) in _export_kinds(repo, package, names).items():
            if kind in ("class", "constant", "alias"):
                continue
            if (package, name) in OBJECT_EXPORTS_ALLOWED:
                seen.add((package, name))
            elif kind == "value":
                found.add(f"{rel}: __all__ exports unrecognised value {name} (FR-44)")
            else:
                found.add(f"{rel}: __all__ exports function {name} (FR-44)")
        found.update(
            f"{pkg}: {name}: allowlisted but not an exported function or value (FR-44)"
            for pkg, name in OBJECT_EXPORTS_ALLOWED.keys() - seen
            if pkg == package
        )
    return sorted(found)


def _all_node(tree):
    bound = _bindings(tree)
    assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign | ast.AnnAssign)
        and any(
            isinstance(target, ast.Name) and target.id == "__all__"
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        )
    ]
    writes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Name | ast.Attribute | ast.Subscript)
        and isinstance(node.ctx, ast.Store | ast.Del)
        and _all_reference(node)
    ]
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            isinstance(node.func, ast.Attribute)
            and _all_reference(node.func.value)
            or _dotted(node.func, bound)
            in {"setattr", "delattr", "builtins.setattr", "builtins.delattr"}
            and len(node.args) > 1
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "__all__"
        )
    ]
    return assignments[0].value if len(assignments) == len(writes) == 1 and not calls else None


def _all_reference(node):
    return any(
        isinstance(child, ast.Name)
        and child.id == "__all__"
        or isinstance(child, ast.Attribute)
        and child.attr == "__all__"
        or isinstance(child, ast.Subscript)
        and isinstance(child.slice, ast.Constant)
        and child.slice.value == "__all__"
        for child in ast.walk(node)
    )


def _is_literal(node):
    try:
        ast.literal_eval(node)
    except (ValueError, TypeError):
        return False
    return True


def _export_kinds(repo, package, names):
    """Each name `package` exports: what it resolves to through re-exports and aliases, and that
    target's kind (function, class, constant, alias, value, module or None: unresolved).
    """
    modules, definitions, origins = {}, {}, {}
    for name, value in vars(builtins).items():
        definitions[f"builtins.{name}"] = (
            "class" if isinstance(value, type) else "function" if callable(value) else "constant"
        )
    for owner in PROJECTS:
        for rel, text in _files(os.path.join(repo, owner, "src"), ".py"):
            module = rel.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
            _definitions(
                module,
                ast.parse(text),
                modules,
                definitions,
                origins,
                package=rel.endswith("/__init__.py"),
            )
    kinds = {}
    for name in names:
        original, resolved, seen = f"{package}.{name}", f"{package}.{name}", set()
        while resolved not in seen:
            seen.add(resolved)
            resolved = _resolve_boundary(resolved, modules)
            if resolved in definitions:
                break
            module, _, member = resolved.rpartition(".")
            if module in modules or module.split(".")[0] not in sys.stdlib_module_names:
                break
            base = os.path.join(sysconfig.get_path("stdlib"), *module.split("."))
            for path, is_package in (
                (base + ".py", False),
                (os.path.join(base, "__init__.py"), True),
            ):
                if os.path.isfile(path):
                    with open(path, encoding="utf-8") as source:
                        _definitions(
                            module,
                            ast.parse(source.read()),
                            modules,
                            definitions,
                            origins,
                            package=is_package,
                        )
                    break
            else:
                break
            seen.discard(resolved)
        kind = definitions.get(original) or definitions.get(resolved)
        if resolved in modules or resolved in sys.stdlib_module_names:
            kind = "module"
        kinds[name] = (origins.get(resolved, resolved), kind)
    return kinds


def _definitions(module, tree, modules, definitions, origins, *, package):
    bound = {name: f"builtins.{name}" for name in vars(builtins)}
    parent = (module if package else module.rpartition(".")[0]).split(".")
    for node in [
        *tree.body,
        *(
            node
            for node in _at_import(tree)
            if isinstance(node, ast.Import | ast.ImportFrom) and node not in tree.body
        ),
    ]:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            key = f"{module}.{node.name}"
            bound[node.name] = key
            definitions[key] = "class" if isinstance(node, ast.ClassDef) else "function"
            origins[key] = key
            if isinstance(node, ast.ClassDef):
                for method in node.body:
                    if isinstance(method, ast.FunctionDef | ast.AsyncFunctionDef):
                        definitions[f"{key}.{method.name}"] = "function"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                bound[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom):
            source = node.module or ""
            if node.level:
                source = ".".join([*parent[: len(parent) - node.level + 1], source]).rstrip(".")
            for alias in node.names:
                if (
                    alias.name == "*"
                    and source in sys.builtin_module_names
                    and source in sys.modules
                ):
                    for name, value in vars(sys.modules[source]).items():
                        bound[name] = f"{source}.{name}"
                        definitions[f"{source}.{name}"] = (
                            "class"
                            if isinstance(value, type)
                            else "function"
                            if callable(value)
                            else "constant"
                        )
                else:
                    bound[alias.asname or alias.name] = f"{source}.{alias.name}"
        elif isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None:
            reference = (
                _dotted(node.value, bound)
                if isinstance(node.value, ast.Name | ast.Attribute)
                else ""
            )
            for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                if not isinstance(target, ast.Name):
                    continue
                key = f"{module}.{target.id}"
                origins[key] = origins.get(reference, reference) if reference else key
                if reference and reference not in definitions:
                    bound[target.id] = reference
                    definitions.pop(key, None)
                else:
                    definitions[key] = (
                        definitions[reference]
                        if reference
                        else "constant"
                        if _constant_value(node.value, bound, definitions)
                        else _assigned_kind(node, target.id)
                    )
                    bound[target.id] = key
    modules[module] = bound


def _constant_value(node, bound, definitions):
    if _is_literal(node) or definitions.get(_dotted(node, bound)) == "constant":
        return True
    return isinstance(node, ast.BinOp) and all(
        _constant_value(part, bound, definitions) for part in (node.left, node.right)
    )


GENERICS = frozenset(
    {"Annotated", "Callable", "Iterable", "Iterator", "Literal", "Mapping", "Optional", "Sequence"}
    | {"Union", "dict", "frozenset", "list", "set", "tuple", "type"}
)


def _assigned_kind(node, name):
    """Kind of a value _constant_value rejects: function (a lambda); constant (an ALL_CAPS tuple,
    list or set of literals and dotted names); alias (annotated TypeAlias, a union or a subscripted
    generic); else value, whatever its annotation (a call such as tuple() or os.path.join() too).
    """
    value = node.value
    if isinstance(value, ast.Lambda):
        return "function"
    if name.isupper() and _references(value):
        return "constant"
    if _name(getattr(node, "annotation", None)).endswith("TypeAlias") or _typing(value):
        return "alias"
    return "value"


def _references(node):
    return isinstance(node, ast.Tuple | ast.List | ast.Set) and all(
        _is_literal(e) or _name(e) for e in node.elts
    )


def _name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and (base := _name(node.value)):
        return f"{base}.{node.attr}"
    return ""


def _typing(node, *, operand=False):
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _typing(node.left, operand=True) and _typing(node.right, operand=True)
    if isinstance(node, ast.Subscript):
        return _name(node.value).rpartition(".")[2] in GENERICS
    return operand and (bool(_name(node)) or isinstance(node, ast.Constant) and node.value is None)


def session_interface_snapshot(repo: str) -> list[str]:
    """The session's public interface equals SESSION_INTERFACE. Only a deliberate, reviewed
    interface change changes it on purpose, rewriting the file from _interface().
    """
    return _snapshot_drift(repo, SESSION_INTERFACE, SESSION_INTERFACE_MODULES)


def delegate_interface_snapshot(repo: str) -> list[str]:
    """The delegate's public interface equals DELEGATE_INTERFACE. Only a deliberate, reviewed
    interface change changes it on purpose, rewriting the file from _interface().
    """
    return _snapshot_drift(repo, DELEGATE_INTERFACE, DELEGATE_INTERFACE_MODULES)


def _snapshot_drift(repo: str, path: str, modules: tuple[str, ...]) -> list[str]:
    try:
        with open(os.path.join(repo, path), encoding="utf-8") as f:
            want = json.load(f)
    except FileNotFoundError:
        return [f"{path}: missing"]
    have = _interface(modules)
    return [
        f"{path}: {key}: {want.get(key, 'absent')} -> {have.get(key, 'absent')}"
        for key in sorted(want.keys() | have.keys())
        if want.get(key) != have.get(key)
    ]


def _interface(modules: tuple[str, ...]) -> dict[str, str]:
    """Each export of `modules` and its classes' public members: kind, bases, enum values and
    parameters (name, kind, default); annotations are left out (their text varies by Python
    version).
    """
    found = {}
    for name in modules:
        module = importlib.import_module(name)
        for export in module.__all__:
            value = getattr(module, export)
            found[f"{name}.{export}"] = _interface_entry(value)
            if inspect.isclass(value) and not issubclass(value, enum.Enum):
                for member, described in _interface_members(value).items():
                    found[f"{name}.{export}.{member}"] = described
    return found


def _interface_entry(value) -> str:
    if inspect.isclass(value) and issubclass(value, enum.Enum):
        return f"enum {[member.value for member in value]}"
    if inspect.isclass(value):
        bases = ", ".join(_qualified(base) for base in value.__bases__)
        return f"class({bases}){_parameters(value)}"
    if callable(value):
        return f"def{_parameters(value)}"
    return f"value {_stable(value)}"


def _interface_members(cls) -> dict[str, str]:
    found = {}
    for klass in reversed(cls.__mro__):
        if not klass.__module__.startswith("ensemble."):
            continue
        names = set(inspect.get_annotations(klass)) | _initialized_attributes(klass)
        found.update({name: "attribute" for name in names if not name.startswith("_")})
        for name, member in vars(klass).items():
            if name.startswith("_"):
                continue
            if isinstance(member, property):
                found[name] = "property"
            elif isinstance(member, (classmethod, staticmethod)):
                found[name] = f"{type(member).__name__}{_parameters(member.__func__)}"
            elif inspect.isfunction(member):
                found[name] = f"def{_parameters(member)}"
            else:
                found[name] = "attribute"
    return found


def _initialized_attributes(cls) -> set[str]:
    constructor = vars(cls).get("__init__")
    if constructor is None:
        return set()
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(constructor)))
    except (OSError, TypeError):
        return set()
    function = tree.body[0]
    if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
        return set()
    positional = function.args.posonlyargs + function.args.args
    if not positional:
        return set()
    instance = positional[0].arg
    found = set()
    pending = list(function.body)
    while pending:
        node = pending.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            continue
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Store)
            and isinstance(node.value, ast.Name)
            and node.value.id == instance
        ):
            found.add(node.attr)
        pending.extend(ast.iter_child_nodes(node))
    return found


def _parameters(value) -> str:
    try:
        signature = inspect.signature(value)
    except (TypeError, ValueError):
        return "(?)"
    return (
        "("
        + ", ".join(
            f"{p.name}:{p.kind.name}"
            + ("" if p.default is inspect.Parameter.empty else f"={_stable(p.default)}")
            for p in signature.parameters.values()
        )
        + ")"
    )


def _stable(value) -> str:
    if isinstance(value, enum.Enum):
        return repr(value.value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return repr(value)
    if (
        isinstance(value, types.UnionType | types.GenericAlias)
        or type(value).__module__ == "typing"
    ):
        return str(value)
    if isinstance(value, tuple | list | frozenset):
        return f"{type(value).__name__}({', '.join(sorted(_stable(v) for v in value))})"
    if hasattr(value, "__qualname__"):
        return _qualified(value)
    return type(value).__name__


def _qualified(value) -> str:
    return f"{value.__module__}.{value.__qualname__}"
