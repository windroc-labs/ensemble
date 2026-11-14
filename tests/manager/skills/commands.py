"""skills/manage/SKILL.md as a manager reads them."""

import os
import re
import shlex

from tests.manager.case import REPO

SKILLS = os.path.join(REPO, "skills")
BIN = os.path.join(REPO, "bin")
COMMAND = re.compile(r"`(ensemble-[a-z]+\b[^`]*)`")
FLAG = re.compile(r"(?<![\w-])--[a-z][a-z-]*")
REFERENCE = re.compile(r"\[([A-Z][a-z]+)\]\(#[a-z-]+\)")
RULES = re.compile(r"^  - Bash\((.+)\)$", re.M)
SKILL_DIR = "${CLAUDE_SKILL_DIR}"
LAUNCHES = {
    "worker": ("<name>", "ensemble-session", "start", "<name>"),
    "reopen": ("<ID>", "ensemble-session", "start", "<ID>"),
    "issue": ("<ID>", "ensemble-orchestrator", "router", "--issue", "<ID>", "--tui"),
    "role": ("<role>", "ensemble-delegate", "run", "--role", "<role>", "--detach"),
}
PATH = re.compile(
    r"`((?:\$\{CLAUDE_SKILL_DIR\}/)?[\w.-]+(?:/[\w.-]+)*\.(?:md|toml|py)|\$\{CLAUDE_SKILL_DIR\}/[\w./-]+)`"
)


def text(skill: str) -> str:
    with open(os.path.join(SKILLS, skill, "SKILL.md"), encoding="utf-8") as f:
        return f.read()


def body(skill: str) -> str:
    return text(skill).split("\n---\n", 1)[1]


def rules(skill: str) -> list[str]:
    return RULES.findall(text(skill).split("\n---\n", 1)[0])


def sections(skill: str) -> list[str]:
    return re.findall(r"^## (.+)$", text(skill), re.M)


def section(skill: str, name: str) -> str:
    return re.search(rf"^## {name}\n(.*?)(?=^## |\Z)", text(skill), re.M | re.S)[1]


def references(skill: str) -> list[str]:
    return REFERENCE.findall(text(skill))


def spans(source: str) -> list[list[list[str]]]:
    """Each backticked example's layer commands, each as its words up to a bare `--` that hands the
    rest to a client; a nested `-- ensemble-…` starts the next command.
    """
    found = []
    for span in COMMAND.findall(source):
        words = span.split()
        starts = [i for i, w in enumerate(words) if w.startswith("ensemble-")]
        commands = []
        for start, end in zip(starts, [*starts[1:], len(words)], strict=True):
            command = words[start:end]
            command = command[: command.index("--")] if "--" in command else command
            kind = (
                command[2] if command[:2] == ["ensemble-crew", "start"] and len(command) > 2 else ""
            )
            key = LAUNCHES.get(kind, ("",))[0]
            if key in command:
                at = command.index(key)
                tail = command[at + 1 :]
                commands.append(command[: at + 1])
                commands.append([*LAUNCHES[kind][1:], *tail])
            else:
                commands.append(command)
        found.append(commands)
    return found


def examples(source: str) -> list[list[str]]:
    return [command for commands in spans(source) for command in commands]


def written(source: str) -> list[list[str]]:
    """Each backticked command up to a bare `--`."""
    found = []
    for span in COMMAND.findall(source):
        words = span.split()
        found.append(words[: words.index("--")] if "--" in words else words)
    return found


def subcommand(command: list[str]) -> tuple[str, ...]:
    depth = {("ensemble-orchestrator", "issue"): 3, ("ensemble-crew", "start"): 2}.get(
        tuple(command[:2]), 1
    )
    found = []
    for word in command[1 : depth + 1]:
        if not re.fullmatch(r"[a-z][a-z-]*", word):
            break
        found.append(word)
    return tuple(found)


def flags(command: list[str]) -> list[str]:
    return FLAG.findall(" ".join(command[1:]))


def paths(skill: str) -> list[str]:
    """Each file the skill names: ${CLAUDE_SKILL_DIR}/… in its directory, else the plugin root's."""
    own = os.path.join(SKILLS, skill)
    return [
        os.path.normpath(os.path.join(own, p.removeprefix(SKILL_DIR + "/")))
        if p.startswith(SKILL_DIR)
        else os.path.join(REPO, p)
        for p in PATH.findall(body(skill))
    ]


def example(skill: str, prefix: str, *having: str) -> str:
    """skill's command prefix, else its one starting with prefix and holding having."""
    spans = COMMAND.findall(body(skill))
    if not having and prefix in spans:
        return prefix
    [found] = sorted({s for s in spans if s.startswith(prefix) and all(h in s for h in having)})
    return found


def fill(command: str, *keep: str, **values: str) -> list[str]:
    """command's argv: a `[...]` dropped unless its first word is in keep, `<a b>` values["a_b"]."""

    def option(match: re.Match) -> str:
        return match[1] if match[1].split()[0] in keep else ""

    def raw(text: str) -> str:
        return re.sub(r"<([^<>]+)>", lambda m: values[m[1].replace(" ", "_")], text)

    command = re.sub(r"\[([^][]+)\]", option, command.replace("…", ""))
    command = re.sub(r"'([^']*)'", lambda m: shlex.quote(raw(m[1])), command)
    return shlex.split(re.sub(r"<([^<>]+)>", lambda m: shlex.quote(raw(m[0])), command))
