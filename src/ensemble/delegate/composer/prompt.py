"""The agent run's prompt (render) and the section reference check its texts pass
(reference_errors).

Guide and principles get {{role}}, its anchor, {{task}} (the named task and its file; none → the
guide's default text) and {{language}} (unset → each line holding it is dropped); a destination gets
{{deliverable}}, <Workdir>/<its path in the workdir>. Only a `local` destination
returns the deliverable in the outcome: any other publishes it, and its prompt names neither outcome
field; on Docker, where the harness cannot read the agent's files, a returning one is INLINE and no
text names `deliverable_file`. Every other run value is a PARAMETERS name,
its value given once in # Parameters. The run reports in its replies (RETURN): PROGRESS mark lines
and one OUTCOME block, which the Delegate parses.

References: a link's target is `#a`, `a` an anchor of the prompt (link text `A › B`: a path there),
or a `<scheme>://` URL; a task or method file's code span may be followed by ` › ` and a path in
that file. Any other `›`, or a section title in parentheses, outside fences, code spans and links is
an error. Only a run using a team dir file is checked; the error names the custom file to blame.
"""

import os
import re
from collections.abc import Mapping

from ensemble.delegate.composer import run as runs
from ensemble.delegate.composer import teams
from ensemble.delegate.composer.base import (
    CODE_SPAN,
    FILE_REF,
    LINK,
    METHOD,
    OUTPUT,
    PAREN,
    SCHEME,
    anchor,
    fill,
    outline,
    read_text,
)
from ensemble.delegate.composer.types import ConfigError, RunConfig, RunParams, Team
from ensemble.delegate.event import OUTCOME, PROGRESS
from ensemble.delegate.event.outcome import DEFAULT_HOST, SAVES_DELIVERABLE
from ensemble.utils import unfenced

RESUME = (
    "Resumed agent run after an interruption. These rules are current and may have changed since this session "
    "started. The input below is current: it adds to this session's earlier input. Continue the task this "
    "session already picked or was given, following your task file's `## Resume` section; never pick it "
    "again.\n\n"
)
RULE = (
    "Each name below stands for its value. Run every command with each name, nested ones included, replaced by its "
    "value and written out in full, no name or shell variable left."
)
PARAMETERS = {
    "<Workdir>": "workdir",
    "<tasks>": "tasks",
    "<team>": "team",
    "<role team>": "role_team",
    "<gate>": "gate",
    "<out-repo>": "output.repo",
    "<out-branch>": "output.branch",
    "<out-dir>": "output.dir",
    "<out-host>": "output.host",
}
RETURN = (
    "## Return\n\nYour replies carry your reports:\n\n"
    f"- **Progress:** at each `[{PROGRESS}:<name>] …` line in your steps, before calling the next tool, write that line in a reply as a line of its own, outside any code block: the mark, then your report, e.g. `[{PROGRESS}:start] <what that line asks you to report>`.\n"
    f'- **Outcome:** once everything else is done and all background work and every agent you spawned have finished (wait for each and collect its result), your last reply is one fenced code block whose info string is `{OUTCOME}`, with no text before or after it, holding the outcome as one JSON object of the fields in [Output](#output), e.g. `{{"status": "done", "title": "<one line>", "summary": "<text>"}}`.\n'
)
INLINE = (
    "## Destination\n\nPut the deliverable inline in the outcome's `deliverable`; publish, post or save it "
    "nowhere else. Leave `url` empty.\n"
)


def render(run: RunConfig, params: RunParams, *, team: Team | None = None) -> str:
    team = teams.team_or_default(team)
    names = {
        "role": run.role_title,
        "role_anchor": anchor(run.role_title),
        "language": run.language,
    }
    names |= {"task": f"`{run.task}` (`<tasks>/{run.task}.md`)"} if run.task else {}
    if not (charter := teams.charter(team, run.role)):
        raise ConfigError(f"missing file roles/{run.role}.md")
    role, rel = charter
    guide, principles = (
        read_text(os.path.join(team.root, f)) for f in ("guide.md", "principles.md")
    )
    if not run.language:
        principles = _without(principles, "{{language}}")
    texts = {
        "guide.md": fill(guide, names, "guide.md"),
        "principles.md": fill(principles, names, "principles.md"),
        rel: fill(role, {}, rel),
    }
    parts = list(texts.items())
    for name in run.templates:
        body, label = teams.read(team, "templates", name)
        f = _fence(body)
        parts.append((label, f"# Template: `templates/{name}.md`\n\n{f}markdown\n{body}{f}\n"))
    output, dest = os.path.join(team.root, OUTPUT), f"destinations/{run.output['type']}.md"
    if not os.path.isfile(os.path.join(output, dest)):
        raise ConfigError(f"no destination {run.output['type']!r} ({OUTPUT}/{dest})")
    inside = os.path.relpath(os.path.realpath(params.deliverable), os.path.realpath(params.workdir))
    rules = fill(read_text(os.path.join(output, "output.md")), {}, "output.md")
    destination = fill(
        read_text(os.path.join(output, dest)), {"deliverable": f"<Workdir>/{inside}"}, dest
    )
    if run.output["type"] not in SAVES_DELIVERABLE:
        rules = _without(rules, "`deliverable`")
    elif params.docker:
        rules, destination = _without(rules, "`deliverable_file`"), INLINE
    texts |= {"output.md": rules, dest: destination, "Return": RETURN}
    parts += [("output.md", rules + "\n" + destination), ("Return", RETURN)]
    joined = [part for _, part in parts]
    joined.insert(1, _parameters(run, params, texts, team))
    own = _unique((RESUME if params.resume else "") + "\n".join(joined) + "\n# Input\n")
    if teams.used(team, run.role, run.templates):
        _check_prompt(team, run, own, dict(parts))
    for command in runs.approved(run, params.workdir, team, docker=params.docker):
        if command.quoted:
            own = own.replace(command.text, command.rule.removesuffix(" *"))
    return own + f"\n{params.input.strip()}\n"


def reference_errors(
    own: str,
    tasks: Mapping[str, str],
    methods: Mapping[str, str],
    *,
    texts: Mapping[str, str] | None = None,
    labels: Mapping[str, str] | None = None,
) -> list[str]:
    outline_ = outline(own)
    anchors = {a for _, _, a, _ in outline_ if a}
    entries, titles = {}, {path[-1] for _, path, _, _ in outline_}
    for _, path, a, _ in outline_:
        entries.setdefault(path, set()).add(a)
    inside = {
        (kind, name): {path[1:] for _, path, _, _ in outline(text) if len(path) > 1}
        for kind, files in (("tasks", tasks), ("methods", methods))
        for name, text in files.items()
    }
    named = {"tasks": list(tasks), "methods": []}
    labels = labels or {}
    checked = (
        (dict(texts) if texts is not None else {"prompt": own})
        | {labels.get(t, f"tasks/{t}.md"): x for t, x in tasks.items()}
        | dict(methods)
    )
    errors = []
    for label, text in checked.items():
        for _, line in unfenced(text):
            kept, pos = [], 0
            for m in CODE_SPAN.finditer(line):
                kept.append(line[pos : m.start()] + " ")
                pos = m.end()
                if not (ref := FILE_REF.fullmatch(m[0])) or not line.startswith(" › ", pos):
                    continue
                kind = ref[1] or ref[2]
                names = (
                    [(kind, n) for n in named[kind]]
                    if ref[3] == "<task>"
                    else [(kind, ref[3] if kind == "tasks" else f"methods/{ref[3]}.md")]
                )
                found = names and all(n in inside for n in names)
                best = (
                    _task_path(line[pos:], set.intersection(*(inside[n] for n in names)))
                    if found
                    else None
                )
                if not best or line[pos + len(best) :].startswith(" › "):
                    errors.append(f"{label}: no such {kind[:-1]} file path: {line.strip()}")
                    pos = len(line)
                    break
                pos += len(best)
            left = "".join(kept) + line[pos:]
            for t, target in LINK.findall(left):
                if target.startswith("#"):
                    if target[1:] not in anchors:
                        errors.append(f"{label}: no such anchor: [{t}]({target})")
                    elif " › " in t and target[1:] not in entries.get(tuple(t.split(" › ")), ()):
                        errors.append(
                            f"{label}: not a path of the prompt at that anchor: [{t}]({target})"
                        )
                elif not SCHEME.match(target):
                    errors.append(f"{label}: relative link target: [{t}]({target})")
            plain = LINK.sub(" ", left)
            if "›" in plain or any(name in titles for name in PAREN.findall(plain)):
                errors.append(f"{label}: bare reference: {line.strip()}")
    return errors


def _task_path(after: str, paths) -> str | None:
    best = None
    for path in paths:
        want = " › " + " › ".join(path)
        if (
            after.startswith(want)
            and not re.match(r"\w", after[len(want) :])
            and (not best or len(want) > len(best))
        ):
            best = want
    return best


def _check_prompt(team: Team, run: RunConfig, own: str, parts: Mapping[str, str]) -> None:
    used = teams.used(team, run.role, run.templates)
    paths = {t: teams.path_for(team, "tasks", t) for t in teams.index(run.role, team)}
    tasks = {t: read_text(p) for t, p in paths.items()}
    labels = {t: team.label("tasks", t, p) for t, p in paths.items()}
    named = dict.fromkeys(METHOD.findall(own + "".join(tasks.values())))
    methods = {}
    for m in named:
        if os.path.isfile(path := os.path.join(team.root, "methods", f"{m}.md")):
            methods[f"methods/{m}.md"] = read_text(path)
    errors = reference_errors(own, tasks, methods, texts=parts, labels=labels)
    if not errors:
        return
    custom = [
        label for label in [*parts, *labels.values()] if os.path.isabs(label)
    ]  # built-in ones are relative
    if first := next((e for e in errors if any(e.startswith(f"{c}: ") for c in custom)), None):
        raise ConfigError(first)
    error, blame = errors[0], used[0]
    line = error.split(": ", 2)[-1]
    if ": no such task file path: " in error and "`<tasks>/<task>.md`" in line:
        blame = next(
            (
                paths[t]
                for t in paths
                if team.custom(paths[t])
                and reference_errors(own, {t: tasks[t]}, {}, texts={"x": line})
            ),
            blame,
        )
    elif ": bare reference: " in error:
        want = set(PAREN.findall(line))
        texts = {label: text for label, text in parts.items() if label in custom}
        texts |= {paths[t]: tasks[t] for t in paths if team.custom(paths[t])}
        blame = next(
            (
                label
                for label, text in texts.items()
                if any(heading and path[-1] in want for _, path, _, heading in outline(text))
            ),
            blame,
        )
    raise ConfigError(f"{blame}: breaks a reference in {error}")


def _parameters(run: RunConfig, params: RunParams, texts: Mapping[str, str], team: Team) -> str:
    texts = dict(texts) | {
        label: body
        for body, label in (teams.read(team, "tasks", t) for t in teams.index(run.role, team))
    }
    named = dict.fromkeys(f"methods/{m}.md" for t in texts.values() for m in METHOD.findall(t))
    texts |= {
        rel: read_text(path)
        for rel in named
        if os.path.isfile(path := os.path.join(team.root, rel))
    }
    values = {
        "workdir": _code(os.path.abspath(params.workdir)),
        "gate": _code(run.gate or "none"),
    } | {k: _code(v) for k, v in runs.team_paths(run, team, docker=params.docker).items()}
    values |= {
        f"output.{k}": _code(str(v)) for k, v in ({"host": DEFAULT_HOST} | run.output).items()
    }
    lines = []
    for name, source in PARAMETERS.items():
        where = next((label for label, t in texts.items() if name in t), None)
        if where and source not in values:
            raise ConfigError(
                f"{name} is used in {where} but Parameters doesn't define it ({source} is not set)"
            )
        if where or name in ("<Workdir>", "<tasks>"):
            lines.append(f"- `{name}`: {values[source]}\n")
    return f"# Parameters\n\n{RULE}\n\n" + "".join(lines)


def _without(text: str, placeholder: str) -> str:
    return "".join(line for line in text.splitlines(keepends=True) if placeholder not in line)


def _fence(text: str, least: int = 3) -> str:
    ticks = max([len(r) + 1 for r in re.findall(r"`+", text)] + [least])
    return "`" * ticks


def _code(value: str) -> str:
    f, pad = _fence(value, 1), " " if "`" in value else ""
    return f"{f}{pad}{value}{pad}{f}"


def _unique(text: str) -> str:
    lines, taken, top, ids = text.split("\n"), set(), "", {}
    for i, path, own, heading in outline(text):
        if not heading:
            continue
        title, first = path[-1], lines[i].startswith("# ")
        top = title if first else top
        if own in taken:
            base = anchor(title if first else f"{top} {title}")
            own, n = base, 2
            while own in taken:
                own, n = f"{base}-{n}", n + 1
            ids[i] = own
        taken.add(own)
    for i, id_ in reversed(ids.items()):
        lines.insert(i, f'<a id="{id_}"></a>')
    return "\n".join(lines)
