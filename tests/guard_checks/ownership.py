"""Client ownership checks: which layer may name a client, tool or display."""

import ast
import io
import os
import re
import tokenize

from tests.guard_checks.common import (
    _bindings,
    _dotted,
    _files,
    _project_files,
    _source_files,
    _strings,
)
from tests.guard_data.delegate import CONFIG_FILE, DELEGATE_SRC
from tests.guard_data.orchestrator import (
    CLIENT_LIMIT_TABLES,
    HANDOFF_HOME,
    ORCH,
    ORCH_FILES,
    ORCH_PROJECT,
)
from tests.guard_data.session import (
    CLIENTS_HOME,
    PLUGIN_SOURCE_CONFIG,
    ROLE_WORDS_ALLOWED,
    SESSION_SRC,
    STATUS_LOG_CLOCK,
    STATUS_LOG_HOME,
    TMUX_HOME,
)
from tests.guards import PKG, PROJECTS

CLIENT = re.compile(r"claude|codex|opencode", re.I)


TOML_CODE = re.compile(r"""(?:[^#"']|"(?:\\.|[^"\\])*"|'[^']*')*""")


TOML_HEADER = re.compile(r"\s*\[\s*(?:clients|policy)\.[a-z][a-z0-9_]*(\.[^\]]*)?\]\s*\Z")


TOML_IMPLEMENTATION = re.compile(
    r'\s*implementation\s*=\s*"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*"\s*\Z'
)


TOML_CLIENT = re.compile(r'\s*client\s*=\s*"[a-z][a-z0-9_]*"\s*\Z')


TOML_PLUGINS = re.compile(r"\s*plugins\s*=")


TOML_PLUGIN_SOURCE = re.compile(
    r'"[A-Za-z0-9][A-Za-z0-9._-]{0,63}@[A-Za-z0-9][A-Za-z0-9-]{0,38}/'
    r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}#[a-f0-9]{40}"'
)


TOML_TABLE = re.compile(r"\s*(?:#\s*local:\s*)?\[\s*([^\]\s]+)\s*\]\s*(?:#.*)?\Z")


TOML_LIMIT_KEY = re.compile(r"\s*#\s*local:\s*[a-z][a-z0-9_]*\s*=\s*\d+\s*(?:#.*)?\Z")


LOWER = tuple(p for p in PROJECTS if p != ORCH_PROJECT)


DISPLAY_NAMES = re.compile(
    r"(?<![a-zA-Z0-9])(?:pane|opener|split_from)(?![a-zA-Z0-9])|--split|@record|@pane|@opener"
)


# the orchestrator's package, project path or config section; "orchestrator" alone is a word
LINEAR = re.compile(rf"(?<![\w.]){PKG}[./]{ORCH}\b|(?<![\w-]){ORCH_PROJECT}/|\[{ORCH}[.\]]")


TOOL_STRINGS = ("tmux", "clients")


TOML_STRING = re.compile(r""""((?:\\.|[^"\\])*)"|'([^']*)'""")


HANDOFF = re.compile(r"Handoff from|## (?:Source|Instructions|Comments)")


def _code_words(text, tree):
    words = {
        (*token.start, token.string)
        for token in tokenize.generate_tokens(io.StringIO(text).readline)
        if token.type == tokenize.NAME
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            words.add((node.lineno, node.col_offset, node.value))
        elif isinstance(node, ast.Name):
            words.add((node.lineno, node.col_offset, node.id))
        elif (
            isinstance(node, ast.Attribute)
            and node.end_lineno is not None
            and node.end_col_offset is not None
        ):
            words.add((node.end_lineno, node.end_col_offset - len(node.attr), node.attr))
    yield from ((line, value) for line, _, value in sorted(words))


def session_names(repo: str) -> list[str]:
    """Only engine/config.py forwards configuration reads; no parser, reply tag or team text."""
    forbidden = (
        "config_file",
        "tomllib",
        "ensemble.delegate.engine.config",
        "read_project",
        "ensemble-progress",
        "ensemble-outcome",
        "team/",
    )
    return sorted(
        f"{SESSION_SRC}{rel}: names {word}"
        for rel, text in _files(os.path.join(repo, SESSION_SRC), ".py")
        for word in forbidden
        if word in text
    )


def session_roles(repo: str) -> list[str]:
    """The session names no role (a team/roles/*.md stem) and reads no roles config."""
    roles_dir = os.path.join(repo, DELEGATE_SRC, "team", "roles")
    roles = {name.removesuffix(".md") for name in os.listdir(roles_dir) if name.endswith(".md")}
    words = {**dict.fromkeys(roles, "names role"), "roles": "reads role config"}
    found = []
    for rel, text in _files(os.path.join(repo, SESSION_SRC), ".py"):
        for line, word in _code_words(text, ast.parse(text, rel)):
            if word in words and (rel, word) not in ROLE_WORDS_ALLOWED:
                found.append(f"{SESSION_SRC}{rel}:{line}: {words[word]} {word}")
    for rel, text in _files(os.path.join(repo, SESSION_SRC), ".toml"):
        for line, raw in enumerate(text.splitlines(), 1):
            code = TOML_CODE.match(raw).group(0)
            found += [
                f"{SESSION_SRC}{rel}:{line}: {words[word]} {word}"
                for word in sorted(words)
                if re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", code)
                and (rel, word) not in ROLE_WORDS_ALLOWED
            ]
    return sorted(dict.fromkeys(found))


def delegate_package_names(repo: str) -> list[str]:
    """Delegate owns package resources and never discovers sibling projects."""
    base = "src/ensemble/delegate/"
    forbidden = {"read_project", "import_roots", "DELEGATE", "IMPORT_ROOTS"}
    found = set()
    for rel, text in _files(os.path.join(repo, base), ".py"):
        tree = ast.parse(text, rel)
        bound = _bindings(tree)
        for node in ast.walk(tree):
            name = (
                node.id
                if isinstance(node, ast.Name)
                else node.attr
                if isinstance(node, ast.Attribute)
                else node.name
                if isinstance(node, ast.alias | ast.FunctionDef | ast.AsyncFunctionDef)
                else ""
            )
            shared_command = (
                isinstance(node, ast.Attribute)
                and isinstance(node.ctx, ast.Load)
                and _dotted(node, bound) == "ensemble.utils.LayerCommand.DELEGATE"
            )
            if name in forbidden and not shared_command:
                found.add(f"{base}{rel}:{node.lineno}: legacy package name {name}")
    return sorted(found)


def manager_sid(repo: str) -> list[str]:
    rel = "src/ensemble/manager/engine/crew.py"
    path = os.path.join(repo, rel)
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as source:
        tree = ast.parse(source.read(), rel)
    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"get", "getenv"}
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "ENSEMBLE_SID"
        ) or (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Load)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == "ENSEMBLE_SID"
        ):
            found.append(f"{rel}:{node.lineno}: reads inherited ENSEMBLE_SID")
    return sorted(found)


def manager_lease(repo: str) -> list[str]:
    rel = "src/ensemble/manager/engine/roster.py"
    path = os.path.join(repo, rel)
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as source:
        tree = ast.parse(source.read(), rel)
    return sorted(
        f"{rel}:{node.lineno}: lease liveness callback"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "_alive"
    )


def manager_display(repo: str) -> list[str]:
    found = set()
    base = "src/ensemble/manager/"
    forbidden = re.compile(
        r"(?<![a-zA-Z0-9_])(?:pane|opener|split_from)(?![a-zA-Z0-9_])|--split|@record"
    )
    for rel, text in _files(os.path.join(repo, base), ".py"):
        tree = ast.parse(text, rel)
        for node in ast.walk(tree):
            value = (
                node.id
                if isinstance(node, ast.Name)
                else node.attr
                if isinstance(node, ast.Attribute)
                else node.arg
                if isinstance(node, ast.arg)
                else node.value
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
                else ""
            )
            if forbidden.search(value):
                found.add(f"{base}{rel}:{node.lineno}: interprets display {value!r}")
    return sorted(found)


def standalone(repo: str) -> list[str]:
    found = set()
    for project in LOWER:
        for rel, text in _project_files(repo, project, "", errors="replace"):
            for number, line in enumerate(text.splitlines(), 1):
                found.update(f"{rel}:{number}: names {w}" for w in LINEAR.findall(line))
    return sorted(found)


def _without_help_text(rel, text):
    if not (rel.endswith("/cli.py") or rel.startswith(TMUX_HOME)):
        return text
    tree = ast.parse(text, rel)
    prose = []
    if rel.endswith("/cli.py"):
        prose = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and re.search(r"Examples[: (]", node.value)
        ]
    else:
        for function in ast.walk(tree):
            if isinstance(function, ast.FunctionDef) and function.name == "display_help":
                prose.extend(
                    node.value
                    for node in ast.walk(function)
                    if isinstance(node, ast.Return)
                    and isinstance(node.value, ast.Constant | ast.JoinedStr)
                )
    original = [line.encode() for line in text.splitlines(keepends=True)]
    masked = original.copy()

    def paint(node, *, restore=False):
        last = node.end_lineno or node.lineno
        for index in range(node.lineno - 1, last):
            start = node.col_offset if index == node.lineno - 1 else 0
            end = node.end_col_offset if index == last - 1 else None
            end = len(original[index].rstrip(b"\r\n")) if end is None else end
            value = original[index][start:end] if restore else b" " * (end - start)
            masked[index] = masked[index][:start] + value + masked[index][end:]

    for node in prose:
        paint(node)
    for node in ast.walk(tree):
        if isinstance(node, ast.FormattedValue):
            paint(node.value, restore=True)
    return b"".join(masked).decode()


def client_names(repo: str) -> list[str]:
    found = set()
    for rel, text in _lower_files(repo, (".py", ".toml"), skip=f"{SESSION_SRC}clients/"):
        text = _without_help_text(rel, text)
        for number, line in enumerate(text.splitlines(), 1):
            if rel.endswith(".toml"):
                code = TOML_CODE.match(line)[0]
                line = code if line[len(code) : len(code) + 1] in ("", "#") else line
                if (
                    TOML_HEADER.match(line)
                    or TOML_IMPLEMENTATION.match(line)
                    or TOML_CLIENT.match(line)
                ):
                    continue
                if rel == PLUGIN_SOURCE_CONFIG and TOML_PLUGINS.match(line):
                    # A pinned repository name is source data, not a client selection.
                    line = TOML_PLUGIN_SOURCE.sub('""', line)
            found.update(f"{rel}:{number}: client name {word}" for word in CLIENT.findall(line))
    return sorted(found)


def clients_key(repo: str) -> list[str]:
    found = set()
    for rel, text in _lower_files(repo, ".py", skip=CLIENTS_HOME):
        found.update(
            f'{rel}:{line}: literal key "clients"'
            for line, s in _strings(ast.parse(text, rel))
            if s == "clients"
        )
    return sorted(found)


def tmux_calls(repo: str) -> list[str]:
    found = set()
    for rel, text in _lower_files(repo, ".py"):
        if rel.startswith(TMUX_HOME):
            continue
        tree = ast.parse(text, rel)
        if rel.startswith(SESSION_SRC):
            found.update(
                f"{rel}:{line}: tmux outside session/hosts/tmux/, viewer/ and engine/tmux_cli.py"
                for line, value in _code_words(text, tree)
                if "tmux" in value.lower()
            )
        else:
            found.update(
                f'{rel}:{line}: str constant "tmux"' for line, s in _strings(tree) if s == "tmux"
            )
    return sorted(found)


def session_display(repo: str) -> list[str]:
    found = set()
    for rel, text in _lower_files(repo, ".py", skip=TMUX_HOME):
        if not (rel.startswith((SESSION_SRC, f"src/{PKG}/utils/")) or rel == CONFIG_FILE):
            continue
        found.update(
            f"{rel}:{line}: interprets display {value!r}"
            for line, value in _code_words(text, ast.parse(text, rel))
            if DISPLAY_NAMES.search(value)
        )
    return sorted(found)


def delegate_display(repo: str) -> list[str]:
    return _display(repo, "delegate", DELEGATE_SRC)


def orchestrator_display(repo: str) -> list[str]:
    return _display(repo, ORCH_PROJECT, ORCH_FILES)


def _display(repo, project, src):
    found = set()
    for rel, text in _lower_files(repo, ".py", projects=(project,)):
        if rel.startswith(src):
            found.update(
                f"{rel}:{line}: interprets display {value!r}"
                for line, value in _code_words(text, ast.parse(text, rel))
                if DISPLAY_NAMES.search(value)
            )
    return sorted(found)


def delegate_tmux(repo: str) -> list[str]:
    return [
        f"{rel}:{number}: names tmux"
        for rel, text in _lower_files(repo, ".py", projects=("delegate",))
        if rel.startswith(DELEGATE_SRC)
        for number, line in enumerate(text.splitlines(), 1)
        if "tmux" in line.lower()
    ]


def status_log_format(repo: str) -> list[str]:
    found = set()
    for top in PROJECTS:
        for rel, text in _source_files(repo, top, ".py"):
            rel = f"src/{rel}"
            if rel != STATUS_LOG_HOME:
                found.update(
                    f"{rel}:{line}: status log line format outside session/engine/status_log.py"
                    for line, s in _strings(ast.parse(text, rel))
                    if STATUS_LOG_CLOCK in s
                )
    return sorted(found)


def orchestrator_text(repo: str) -> list[str]:
    found = set()
    for rel, text in _project_files(repo, ORCH_PROJECT, (".py", ".toml")):
        lines = text.splitlines()
        grammar = rel.endswith(".py") and rel != HANDOFF_HOME
        limits = _client_limit_keys(lines) if rel.endswith(".toml") else set()
        for number, line in enumerate(lines, 1):
            line = line[line.index("=") :] if number in limits else line
            found.update(
                f"{rel}:{number}: client name {w}"
                for w in CLIENT.findall(line.replace("AGENTS.md", " "))
            )
            if grammar:
                found.update(f"{rel}:{number}: Handoff literal {w}" for w in HANDOFF.findall(line))
        strings = (
            _strings(ast.parse(text, rel))
            if rel.endswith(".py")
            else (
                (number, m[1] or m[2])
                for number, line in enumerate(lines, 1)
                for m in TOML_STRING.finditer(TOML_CODE.match(line)[0])
            )
        )
        found.update(
            f'{rel}:{line}: str constant "{s}"' for line, s in strings if s in TOOL_STRINGS
        )
    return sorted(found)


def _client_limit_keys(lines):
    """Numbers of the `# local:` key lines in a CLIENT_LIMIT_TABLES table, its `# local:` header included."""
    table, keys = None, set()
    for number, line in enumerate(lines, 1):
        if header := TOML_TABLE.match(line):
            table = header[1].removeprefix(f"{ORCH}.")
        elif table in CLIENT_LIMIT_TABLES and TOML_LIMIT_KEY.match(line):
            keys.add(number)
    return keys


def _lower_files(repo, suffixes, skip=None, projects=LOWER):
    for project in projects:
        for rel, text in _project_files(repo, project, suffixes):
            if not (skip and rel.startswith(skip)):
                yield rel, text
