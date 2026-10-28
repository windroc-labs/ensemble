"""The interfaces the session harness replaced stay gone from shipped code: no definition or
import of the removed classes and function, no Host or InteractiveProcess subclass defining a
removed member, no removed call on a Host.
"""

import ast

from tests.guard_checks.common import _tree_files

SHIPPED = "src/ensemble/"
REMOVED = {"Process", "Interactive", "check_sid"}
HOST_MEMBERS = {
    "terminal",
    "detach",
    "attach",
    "reclaim",
    "events_path",
    "prepare_events",
    "hook_callback",
}
HANDLE_MEMBERS = {
    "alive",
    "wait_exit",
    "kill",
    "identity",
    "token",
    "session_identity",
    "activity",
    "status",
    "mark",
    "row",
    "record",
    "publish",
    "display",
}


def removed_interfaces(repo: str) -> list[str]:
    trees = [
        (rel, ast.parse(text, rel))
        for rel, text in _tree_files(repo, ".py")
        if rel.startswith(SHIPPED)
    ]
    hosts = _subclasses(trees, "Host")
    handles = _subclasses(trees, "InteractiveProcess")
    found = []
    for rel, tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef | ast.FunctionDef) and node.name in REMOVED:
                found.append(f"{rel}:{node.lineno}: defines {node.name}")
            elif isinstance(node, ast.ImportFrom):
                found.extend(
                    f"{rel}:{node.lineno}: imports {alias.name}"
                    for alias in node.names
                    if alias.name in REMOVED
                )
            if isinstance(node, ast.ClassDef):
                found.extend(_members(rel, node, hosts, handles))
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in HOST_MEMBERS
                and "host" in ast.unparse(node.func.value).lower()
            ):
                found.append(f"{rel}:{node.lineno}: calls {ast.unparse(node.func)}")
    return sorted(found)


def _bases(node: ast.ClassDef) -> set[str]:
    return {
        base.attr if isinstance(base, ast.Attribute) else base.id
        for base in node.bases
        if isinstance(base, ast.Attribute | ast.Name)
    }


def _subclasses(trees, root: str) -> set[str]:
    """Class names deriving from `root` (by name) across the shipped tree, `root` excluded."""
    classes = [
        node for _, tree in trees for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
    ]
    found, grown = set(), True
    while grown:
        grown = False
        for node in classes:
            if node.name not in found and _bases(node) & (found | {root}):
                found.add(node.name)
                grown = True
    return found


def _members(rel: str, node: ast.ClassDef, hosts: set[str], handles: set[str]) -> list[str]:
    removed = []
    if node.name in hosts | {"Host"}:
        removed.append(("Host", HOST_MEMBERS))
    if node.name in handles | {"InteractiveProcess"}:
        removed.append(("InteractiveProcess", HANDLE_MEMBERS))
    return [
        f"{rel}:{item.lineno}: {node.name} defines removed {kind} member {item.name}"
        for kind, members in removed
        for item in node.body
        if isinstance(item, ast.FunctionDef) and item.name in members
    ]
