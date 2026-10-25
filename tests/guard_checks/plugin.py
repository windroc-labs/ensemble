"""Plugin manifest checks: each client's manifest (OpenCode's: package.json) ships every skill of the one
ensemble plugin.
"""

import glob
import json
import os
import re

from tests.guards import PROJECTS

MANIFESTS = (".claude-plugin/plugin.json", ".codex-plugin/plugin.json", "package.json")


SKILL_DIRS = ("skills", *(f"{project}/skills" for project in PROJECTS))


SKILLS = ("act-as", "board", "manage")


# OpenCode 1.19.3 session/prompt.ts
SHELL, PLACEHOLDER = re.compile(r"!`[^`]+`"), re.compile(r"\$\d+")


def _skills(repo, dirs):
    return {
        os.path.relpath(path, repo)
        for skills in dirs
        for path in glob.glob(os.path.join(repo, skills, "*", "SKILL.md"))
    }


def plugin_manifests(repo: str) -> list[str]:
    """Each manifest's `skills` paths start with ./ and hold exactly the plugin's skills
    (`skills/` and each project's `skills/`); its name and version are the first manifest's.
    """
    found, first = [], None
    want = _skills(repo, SKILL_DIRS)
    for rel in MANIFESTS:
        if not os.path.isfile(path := os.path.join(repo, rel)):
            found.append(f"{rel}: missing")
            continue
        with open(path, encoding="utf-8") as manifest:
            fields = json.load(manifest)
        paths = fields.get("skills", [])
        paths = [paths] if isinstance(paths, str) else paths
        found += [
            f'{rel}: skills path "{p}" does not start with ./'
            for p in paths
            if not p.startswith("./")
        ]
        shipped = _skills(repo, [p.removeprefix("./") for p in paths if p.startswith("./")])
        found += [f"{rel}: skills miss {skill}" for skill in sorted(want - shipped)]
        found += [
            f"{rel}: skills ship {skill}, not a plugin skill" for skill in sorted(shipped - want)
        ]
        if first is None:
            first = rel, fields
            continue
        found += [
            f'{rel}: {key} "{fields.get(key)}", not {first[0]}\'s "{first[1].get(key)}"'
            for key in ("name", "version")
            if fields.get(key) != first[1].get(key)
        ]
    return found


def plugin_command_templates(repo: str) -> list[str]:
    """Each skill body, the OpenCode plugin's command template (.opencode-plugin/ensemble.js), has
    no shell expansion (!`…`) and no positional placeholder ($1); $ARGUMENTS is the arguments.
    """
    found = []
    for rel in sorted(_skills(repo, SKILL_DIRS)):
        with open(os.path.join(repo, rel), encoding="utf-8") as skill:
            text = skill.read()
        body = text.split("\n---\n", 1)[1] if text.startswith("---\n") else text
        if SHELL.search(body):
            found.append(f"{rel}: !` runs a shell command in an OpenCode command template")
        found += [
            f"{rel}: {match} is an OpenCode command argument placeholder"
            for match in sorted(set(PLACEHOLDER.findall(body)))
        ]
    return found


def plugin_skills(repo: str) -> list[str]:
    """The plugin's skills are exactly SKILLS, each in one place."""
    names = sorted(os.path.basename(os.path.dirname(rel)) for rel in _skills(repo, SKILL_DIRS))
    found = [
        f"skill {name}: shipped {names.count(name)} times"
        for name in sorted(set(names))
        if names.count(name) > 1
    ]
    found += [f"skill {name}: missing" for name in SKILLS if name not in names]
    found += [
        f"skill {name}: not one of {', '.join(SKILLS)}" for name in sorted(set(names) - set(SKILLS))
    ]
    return found
