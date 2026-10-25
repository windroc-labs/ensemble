"""Executable programs belong in their own files, passed by path or stdin."""

import ast
import hashlib
import os
import re
import textwrap

from tests.guard_checks.common import _tree_files
from tests.guard_data import EMBEDDED_PROGRAM_ARGV_RETURNS, EMBEDDED_PROGRAMS_ALLOWED

SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def embedded_programs(repo: str, *, allowed=None) -> list[str]:
    allowed = EMBEDDED_PROGRAMS_ALLOWED if allowed is None else allowed
    found, used = [], set()
    for path, text in _tree_files(repo, ".py"):
        scan = _Scan()
        scan.scope(ast.parse(text), {})
        for line, code, sink in scan.findings():
            key = path, hashlib.sha256(code.encode()).hexdigest()
            if key in allowed:
                used.add(key)
            else:
                found.append(
                    f"{path}:{line}: embedded program ({sink}); "
                    "move it to its own file, passed by path or stdin"
                )
    found.extend(
        f"{path}: stale embedded program exception {digest}"
        for path, digest in allowed.keys() - used
    )
    found.extend(
        f"{path}: embedded program exception needs one-line reason"
        for (path, _), reason in allowed.items()
        if not isinstance(reason, str) or not reason.strip() or "\n" in reason
    )
    return sorted(set(found))


def _nodes(nodes):
    for node in nodes:
        yield node
        if not isinstance(node, SCOPES):
            yield from _nodes(ast.iter_child_nodes(node))


def _resolved(node, env, seen=frozenset()):
    if isinstance(node, ast.Name) and node.id in env:
        marker = id(env), node.id
        if marker not in seen:
            for value, owner in env[node.id]:
                yield from _resolved(value, owner, seen | {marker})
    else:
        yield node, env


def _texts(node, env, seen=frozenset()):
    marker = id(node), id(env)
    if marker in seen:
        return set()
    seen = seen | {marker}
    found = set()
    for value, owner in _resolved(node, env):
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            found.add(value.value)
        elif isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
            found.update(
                a + b
                for a in _texts(value.left, owner, seen)
                for b in _texts(value.right, owner, seen)
            )
        elif isinstance(value, ast.JoinedStr):
            parts = {""}
            for part in value.values:
                choices = _texts(
                    part.value if isinstance(part, ast.FormattedValue) else part, owner, seen
                )
                parts = {a + b for a in parts for b in choices}
            found.update(parts)
        elif isinstance(value, ast.Call):
            name = _name(value.func, owner)
            if value.args and name in {"textwrap.dedent", "dedent"}:
                found.update(textwrap.dedent(text) for text in _texts(value.args[0], owner, seen))
            elif isinstance(value.func, ast.Attribute) and value.func.attr in {
                "strip",
                "lstrip",
                "rstrip",
            }:
                found.update(
                    getattr(text, value.func.attr)()
                    for text in _texts(value.func.value, owner, seen)
                )
    return found


def _name(node, env, seen=frozenset()):
    marker = id(node), id(env)
    if marker in seen:
        return ""
    seen = seen | {marker}
    names = set()
    for value, owner in _resolved(node, env):
        if isinstance(value, ast.Name):
            names.add(value.id)
        elif isinstance(value, ast.Attribute):
            names.add(f"{_name(value.value, owner, seen)}.{value.attr}")
    return next(iter(names)) if len(names) == 1 else ""


def _sequence(nodes, env, seen=frozenset()):
    result = []
    for node in nodes:
        if isinstance(node, ast.Starred):
            marker = id(node), id(env)
            if marker in seen:
                continue
            expanded = list(_resolved(node.value, env))
            if len(expanded) == 1 and isinstance(expanded[0][0], ast.List | ast.Tuple):
                value, owner = expanded[0]
                result.extend(_sequence(value.elts, owner, seen | {marker}))
            continue
        result.append((node, env))
    return result


def _identities(node, env, seen=frozenset()):
    marker = id(node), id(env)
    if marker in seen:
        return set()
    seen = seen | {marker}
    result = set()
    for value, owner in _resolved(node, env):
        if (
            isinstance(value, ast.Call)
            and _name(value.func, owner) in {"Path", "pathlib.Path", "str", "open"}
            and value.args
        ):
            result.update(_identities(value.args[0], owner, seen))
        elif isinstance(value, ast.Constant) and isinstance(value.value, str):
            result.add(("literal", value.value))
        else:
            result.add((id(owner), ast.dump(value)))
    return result


def _interpreter(node, env):
    return _name(node, env) == "sys.executable" or any(
        re.fullmatch(r"python(?:[23](?:\.\d+)?)?|sh|bash", os.path.basename(text))
        for text in _texts(node, env)
    )


class _Scan:
    def __init__(self):
        self.found = set()
        self.written = []
        self.executed = []

    def findings(self):
        for identities, codes, line in self.written:
            if any(identities & paths for paths in self.executed):
                self.found.update((line, code, "written to a file and run") for code in codes)
        return self.found

    def scope(self, tree, inherited, supplied=None, active=()):
        env = dict(inherited)
        body = tree.body if isinstance(tree.body, list) else [tree.body]
        nodes = list(_nodes(body))
        if isinstance(tree, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            for arg in ast.walk(tree.args):
                if isinstance(arg, ast.arg):
                    env[arg.arg] = []
        for node in nodes:
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
                env[node.id] = []
        for node in nodes:
            if isinstance(node, ast.Assign | ast.AnnAssign | ast.NamedExpr):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    value = node.value
                    if isinstance(target, ast.Tuple | ast.List) and isinstance(value, ast.Call):
                        slot = EMBEDDED_PROGRAM_ARGV_RETURNS.get(_name(value.func, env))
                        if slot is not None and slot < len(target.elts):
                            target = target.elts[slot]
                            value = ast.Tuple(elts=value.args)
                    if isinstance(target, ast.Name) and value is not None:
                        env.setdefault(target.id, []).append((value, env))
            elif isinstance(node, ast.Import | ast.ImportFrom):
                for alias in node.names:
                    name = alias.asname or alias.name.split(".")[0]
                    imported = (
                        alias.name
                        if isinstance(node, ast.Import)
                        else f"{node.module}.{alias.name}"
                    )
                    env[name] = [(ast.parse(imported, mode="eval").body, {})]
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                env[node.name] = [(node, env)]
            elif isinstance(node, ast.With | ast.AsyncWith):
                for item in node.items:
                    if isinstance(item.optional_vars, ast.Name):
                        env[item.optional_vars.id] = [(item.context_expr, env)]
        env.update(supplied or {})
        for node in nodes:
            if isinstance(node, ast.Call):
                self.call(node, env, active)
            elif isinstance(node, SCOPES):
                outer = inherited if isinstance(tree, ast.ClassDef) else env
                self.scope(node, outer, active=(*active, id(node)))

    def call(self, node, env, active):
        if _name(node.func, env) in {"exec", "builtins.exec"} and node.args:
            self.found.update((node.lineno, text, "exec()") for text in _texts(node.args[0], env))
        if isinstance(node.func, ast.Attribute) and node.args:
            if node.func.attr == "write_text":
                self.written.append(
                    (_identities(node.func.value, env), _texts(node.args[0], env), node.lineno)
                )
            elif node.func.attr == "write":
                for handle, owner in _resolved(node.func.value, env):
                    if (
                        isinstance(handle, ast.Call)
                        and _name(handle.func, owner) in {"open", "io.open"}
                        and handle.args
                    ):
                        self.written.append(
                            (
                                _identities(handle.args[0], owner),
                                _texts(node.args[0], env),
                                node.lineno,
                            )
                        )
        sequences = [_sequence(node.args, env)]
        for arg in [
            *node.args,
            *(keyword.value for keyword in node.keywords if keyword.arg == "args"),
        ]:
            for value, owner in _resolved(arg, env):
                if isinstance(value, ast.List | ast.Tuple):
                    sequences.append(_sequence(value.elts, owner))
        for sequence in sequences:
            self.command(
                sequence,
                node.lineno,
                _name(node.func, env).split(".")[-1]
                in {
                    "run",
                    "call",
                    "check_call",
                    "check_output",
                    "Popen",
                    "execv",
                    "execve",
                    "execl",
                    "spawnv",
                },
            )
        for function, closure in _resolved(node.func, env):
            if (
                not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda)
                or id(function) in active
            ):
                continue
            parameters = [*function.args.posonlyargs, *function.args.args]
            supplied = {
                arg.arg: [(value, env)] for arg, value in zip(parameters, node.args, strict=False)
            }
            supplied.update({kw.arg: [(kw.value, env)] for kw in node.keywords if kw.arg})
            if function.args.vararg:
                supplied[function.args.vararg.arg] = [
                    (ast.Tuple(elts=node.args[len(parameters) :]), env)
                ]
            self.scope(function, closure, supplied, (*active, id(function)))

    def command(self, sequence, line, executable):
        if not executable:
            return
        for index, (node, env) in enumerate(sequence):
            if not _interpreter(node, env):
                continue
            args = sequence[index + 1 :]
            for position, (arg, owner) in enumerate(args):
                words = _texts(arg, owner)
                if "-c" in words and position + 1 < len(args):
                    code, bindings = args[position + 1]
                    self.found.update(
                        (line, text, "interpreter -c") for text in _texts(code, bindings)
                    )
                    break
                if any(word.startswith("-") for word in words):
                    continue
                self.executed.append(_identities(arg, owner))
                break
        if sequence and executable:
            node, env = sequence[0]
            self.executed.append(_identities(node, env))
