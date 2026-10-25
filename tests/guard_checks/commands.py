"""Layer command checks: who runs which ensemble command, and how Python runs."""

import ast
import os
import re
import tomllib

from tests.guard_checks.common import _bindings, _dotted, _files, _strings, _tree_files
from tests.guard_data import COMMAND_DOCS, COMMAND_FILES, COMMAND_SRC
from tests.guard_data.session import SESSION_COMMAND, SESSION_SRC
from tests.guards import PKG, PROJECTS

COMMAND = "ensemble-"


SPAWNS = re.compile(r"subprocess\.\w+|os\.(?:exec|spawn)\w*")


PYTHON = re.compile(r"\bpython(?:3(?:\.\d+)?)?\b(?:[ \t]+[^\s`'\"]+){0,3}")


PYTHON_OK = re.compile(
    f"\\S+[ \\t]+-P[ \\t]+(?:-m[ \\t]+{PKG}\\.\\w|(?:<(?:role )?team>|\\{{\\{{(?:role_)?team\\}}\\}})/tools/\\w+\\.py(?=[ \\t]|\\Z))"
)


BIN = "bin"


BIN_MODULE = re.compile(r"\bpython3 -P -m ([\w.]+)")


ALLOWED_BASH = re.compile(r"^[ \t]+- Bash\((.+)\)$", re.M)


def _manager_trees(repo):
    base = "manager/src/ensemble/manager/"
    for rel, text in _files(os.path.join(repo, base), ".py"):
        yield base + rel, ast.parse(text)


def manager_commands(repo: str) -> list[str]:
    found = []
    for rel, tree in _manager_trees(repo):
        allowed = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Tuple | ast.List) or not node.elts:
                continue
            values = [v.value if isinstance(v, ast.Constant) else None for v in node.elts]
            listing = values in (
                [SESSION_COMMAND, "ls", "--here", "--records"],
                [SESSION_COMMAND, "ls", "--here", "--records", "--descendants"],
            )
            starter = (
                len(values) >= 3
                and values[:2] == [SESSION_COMMAND, "start"]
                or len(values) >= 5
                and values[:3] == ["ensemble-orchestrator", "router", "--issue"]
                and values[4] == "--tui"
                or len(values) >= 7
                and values[:3] == ["ensemble-delegate", "run", "--role"]
                and values[4:6] == ["--detach", "--name"]
            )
            if listing or starter:
                allowed.add(id(node.elts[0]))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            and ast.get_docstring(node) is not None
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value.startswith(COMMAND)
                and node.value != "ensemble-crew"
                and id(node) not in allowed | docstrings
            ):
                found.append(f"{rel}:{node.lineno}: hardcoded layer command {node.value}")
    return sorted(found)


def manager_status_words(repo: str) -> list[str]:
    found = []
    for rel, tree in _manager_trees(repo):
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if any(word in node.value for word in ("-drive", "needs_input")):
                    found.append(f"{rel}:{node.lineno}: private status vocabulary {node.value!r}")
            elif isinstance(node, ast.Compare):
                parts = list(ast.walk(node))
                if any(
                    isinstance(p, ast.Attribute) and p.attr in {"outcome", "state"} for p in parts
                ) and any(isinstance(p, ast.Constant) and isinstance(p.value, str) for p in parts):
                    found.append(f"{rel}:{node.lineno}: status comparison needs enum members")
    return sorted(found)


def layer_commands(repo: str) -> list[str]:
    found = set()
    for scanned in COMMAND_SRC:
        for rel, text in _files(os.path.join(repo, scanned), ".py"):
            tree = ast.parse(text, rel)
            bound = _bindings(tree)
            for call in ast.walk(tree):
                if isinstance(call, ast.Call) and SPAWNS.fullmatch(_dotted(call.func, bound)):
                    argv = [*call.args, *(k.value for k in call.keywords if k.arg == "args")]
                    words = {
                        (line, s.split()[0])
                        for arg in argv
                        for line, s in _strings(arg)
                        if s.startswith(COMMAND)
                    }
                    found.update(
                        f'{scanned}{rel}:{line}: runs "{word}"'
                        for line, word in words
                        if not (scanned == SESSION_SRC and word == SESSION_COMMAND)
                    )
    return sorted(found)


def python_commands(repo: str) -> list[str]:
    found = set()
    for rel, text in _tree_files(repo, (".toml", ".md")):
        if rel in COMMAND_FILES or rel.endswith(".md") and rel.startswith(COMMAND_DOCS):
            found.update(
                f'{rel}:{number}: python command "{m[0]}"'
                for number, line in enumerate(text.splitlines(), 1)
                for m in PYTHON.finditer(line)
                if not PYTHON_OK.match(m[0])
            )
    return sorted(found)


def bin_commands(repo: str) -> list[str]:
    """Each bin/ command runs exactly one module of its own; each command a skill pre-approves, and
    each config command or gate naming an ensemble- command, is a bin/ command.
    """
    top = os.path.join(repo, BIN)
    names = sorted(os.listdir(top)) if os.path.isdir(top) else []
    found, owners = [], {}
    for name in names:
        with open(os.path.join(top, name), encoding="utf-8") as script:
            modules = BIN_MODULE.findall(script.read())
        if len(modules) != 1:
            found.append(f"{BIN}/{name}: runs {len(modules)} modules, not 1")
        elif not any(
            os.path.isfile(os.path.join(repo, p, "src", *modules[0].split(".")) + ".py")
            for p in PROJECTS
        ):
            found.append(f"{BIN}/{name}: no module {modules[0]}")
        elif modules[0] in owners:
            found.append(f"{BIN}/{name}: runs {modules[0]}, as {BIN}/{owners[modules[0]]} does")
        else:
            owners[modules[0]] = name
    for rel, text in _tree_files(repo, (".md",)):
        if rel.endswith("/SKILL.md") and rel.startswith(COMMAND_DOCS) and text.startswith("---\n"):
            front = text.split("\n---", 1)[0]
            found += [
                f'{rel}: pre-approves "{rule}", not a {BIN}/ command'
                for rule in ALLOWED_BASH.findall(front)
                if rule.split()[0] not in names
            ]
    for rel in COMMAND_FILES:
        if os.path.isfile(path := os.path.join(repo, rel)):
            with open(path, "rb") as config:
                found += [
                    f'{rel}: runs "{command}", not a {BIN}/ command'
                    for command in _commands(tomllib.load(config))
                    if command.startswith(COMMAND) and command.split()[0] not in names
                ]
    return sorted(found)


def _commands(table):
    for key, value in table.items():
        if isinstance(value, dict):
            yield from _commands(value)
        elif key == "gate" and isinstance(value, str):
            yield value
        elif key == "commands" and isinstance(value, list):
            yield from (v for v in value if isinstance(v, str))
