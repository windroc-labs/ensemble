"""Derive test scope from production layer contracts and validate CI results."""

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path


def layer_map(root: Path) -> dict[str, str]:
    with (root / "pyproject.toml").open("rb") as file:
        config = tomllib.load(file)
    tool = config.get("tool", {})
    if not isinstance(tool, dict) or not isinstance(tool.get("importlinter"), dict):
        raise ValueError("Missing or malformed import-linter configuration")
    contracts = tool["importlinter"].get("contracts")
    if not isinstance(contracts, list) or any(not isinstance(item, dict) for item in contracts):
        raise ValueError("Import-linter contracts must be a list of tables")
    candidates = [contract for contract in contracts if contract.get("type") == "layers"]
    if len(candidates) != 1:
        raise ValueError("Expected exactly one production layers contract")
    modules = candidates[0].get("layers")
    if (
        not isinstance(modules, list)
        or not modules
        or any(
            not isinstance(module, str)
            or re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+", module) is None
            for module in modules
        )
        or len(set(modules)) != len(modules)
        or "ensemble.delegate" not in modules
        or modules.index("ensemble.delegate") == 0
    ):
        raise ValueError("Production layers must be unique modules ordered above ensemble.delegate")
    projects = {}
    for module in modules:
        package = root / "src" / module.replace(".", "/")
        if not package.is_dir():
            raise ValueError(f"Layer {module} must map to a source package")
        project = module.split(".")[-1]
        if project in projects:
            raise ValueError(f"Multiple production layers map to project {project}")
        projects[project] = module
    return projects


def _owner(path: str) -> str | None:
    if not isinstance(path, str) or not path or "\x00" in path or "\\" in path:
        return None
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    if len(parts) >= 4 and parts[:2] == ["src", "ensemble"]:
        return parts[2]
    if len(parts) >= 3 and parts[0] == "tests":
        return parts[1]
    if len(parts) >= 3 and parts[0] == "skills":
        return {"manage": "manager", "board": "orchestrator", "act-as": "delegate"}.get(parts[1])
    if len(parts) >= 2 and parts[0] in {"hooks", "launchd"}:
        return {"hooks": "manager", "launchd": "orchestrator"}[parts[0]]
    return parts[0]


# Docs a repository check reads (tests/guard_checks/credentials.py), so they are not docs-only.
CHECKED_DOCS = ("docs/credentials.md",)


def _docs(path: str) -> bool:
    # Root *.md and docs/** but CHECKED_DOCS; skills and team text are not docs.
    if path in CHECKED_DOCS:
        return False
    return _owner(path) == "docs" or (_owner(path) == path and path.endswith(".md"))


def classify(root: Path, paths: list[str], event: str) -> dict:
    projects = layer_map(root)
    full = {"full": True, "docs": False, "broker": True, "projects": list(projects), "reason": ""}
    if event != "pull_request":
        return {**full, "reason": f"Event {event} requires every layer"}
    if not paths:
        return {**full, "reason": "Empty change list requires every layer"}
    boundary = list(projects.values()).index("ensemble.delegate")
    upper = list(projects)[:boundary]
    if all(_docs(path) for path in paths):
        # Upper unit tests check the docs the skills cite; nothing else reads these docs.
        return {
            "full": False,
            "docs": True,
            "broker": False,
            "projects": upper,
            "reason": "Docs-only changes",
        }
    for path in paths:
        if _owner(path) not in upper:
            return {**full, "reason": f"Shared, lower-layer or unsafe path: {path!r}"}
    return {
        "full": False,
        "docs": False,
        "broker": False,
        "projects": upper,
        "reason": "Changes touch only upper-layer projects",
    }


def changed_paths(root: Path, base: str, head: str) -> list[str]:
    output = subprocess.check_output(
        ["git", "-C", str(root), "diff", "--name-only", "--no-renames", "-z", f"{base}...{head}"]
    )
    return [os.fsdecode(path) for path in output.split(b"\x00") if path]


def selected(root: Path, directories: list[str]) -> list[str]:
    value = os.environ.get("TEST_PROJECTS")
    if not value:
        return directories
    try:
        scope = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError("TEST_PROJECTS must be a JSON array of project names") from error
    projects = layer_map(root)
    if (
        not isinstance(scope, list)
        or not scope
        or any(not isinstance(project, str) or project not in projects for project in scope)
        or len(set(scope)) != len(scope)
    ):
        raise ValueError("TEST_PROJECTS must contain nonempty unique contract project names")
    if set(scope) == set(projects):
        return directories
    return [directory for directory in directories if _owner(directory) in scope]


def gate(needs: dict, event: str) -> list[str]:
    if not isinstance(needs, dict):
        return ["CI_NEEDS must be an object containing all required jobs"]
    failures = []
    if event not in ("pull_request", "push"):
        failures.append(f"Unsupported CI event: {event}")
    changes = needs.get("changes")
    output = changes.get("outputs") if isinstance(changes, dict) else None
    full = output.get("full") if isinstance(output, dict) else None
    if full not in ("true", "false") or not isinstance(full, str):
        failures.append("changes.outputs.full must be exactly true or false")
    if event == "push" and full != "true":
        failures.append("changes.outputs.full must be true on push")
    docs = output.get("docs") if isinstance(output, dict) else None
    if docs not in ("true", "false") or not isinstance(docs, str):
        failures.append("changes.outputs.docs must be exactly true or false")
    if docs == "true" and full != "false":
        failures.append("changes.outputs.docs requires full false")
    broker = output.get("broker") if isinstance(output, dict) else None
    if broker not in ("true", "false") or not isinstance(broker, str):
        failures.append("changes.outputs.broker must be exactly true or false")
    if full == "true" and broker != "true":
        failures.append("changes.outputs.broker must be true for full scope")
    if docs == "true" and broker != "false":
        failures.append("changes.outputs.docs requires broker false")
    expected = {
        "changes": "success",
        "lint": "success",
        "repository": "skipped" if full == "false" else "success",
        "unit": "success",
        "coverage": "success" if event == "pull_request" else "skipped",
        "integration": "skipped" if docs == "true" else "success",
        "broker": "success",
        "mini-op": "success",
    }
    for job, wanted in expected.items():
        data = needs.get(job)
        actual = data.get("result") if isinstance(data, dict) else None
        if actual != wanted:
            failures.append(f"{job}: expected {wanted}, got {actual!r}")
    return failures


def main(argv=None, *, root=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event")
    parser.add_argument("--base")
    parser.add_argument("--head")
    parser.add_argument("--paths", nargs="*")
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--gate", action="store_true")
    args = parser.parse_args(argv)
    if args.gate:
        try:
            failures = gate(
                json.loads(os.environ.get("CI_NEEDS", "")), os.environ.get("GITHUB_EVENT_NAME", "")
            )
        except json.JSONDecodeError as error:
            failures = [f"Invalid CI_NEEDS JSON: {error}"]
        for failure in failures:
            print(failure)
        if not failures:
            print("All required CI results passed")
        return int(bool(failures))
    root = root if root is not None else Path(__file__).resolve().parents[1]
    try:
        if not args.event:
            raise ValueError("--event is required for classification")
        if args.paths is not None and (args.base or args.head):
            raise ValueError("Use --paths or --base and --head, not both")
        paths = args.paths
        if args.event == "pull_request" and paths is None:
            if not args.base or not args.head:
                raise ValueError("Pull requests require --paths or both --base and --head")
            paths = changed_paths(root, args.base, args.head)
        result = classify(root, paths or [], args.event)
        print(json.dumps(result))
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as output:
                output.write(
                    f"full={str(result['full']).lower()}\ndocs={str(result['docs']).lower()}\n"
                    f"broker={str(result['broker']).lower()}\n"
                    f"projects={json.dumps(result['projects'])}\n"
                )
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"CI scope failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
