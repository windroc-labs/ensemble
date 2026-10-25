"""Each role's Stage, from a file's [[stage]] tables."""

import tomllib
from collections.abc import Iterator, Mapping
from importlib import resources
from typing import get_args, get_type_hints

from ensemble.orchestrator.engine.flow.types import Stage

PACKAGED = "src/ensemble/orchestrator/engine/flow/workflow.toml"
# Literal: values; else type
TYPES = {k: get_args(t) or t for k, t in get_type_hints(Stage).items()}


class Workflow(Mapping[str, Stage]):
    """Role → Stage; the packaged file's unless load() read one."""

    _stages: Mapping[str, Stage] | None = None
    path: str = PACKAGED  # the stages' file, as errors name it

    def load(self, path: str | None = None) -> Mapping[str, Stage]:
        """A ValueError names path (None: packaged) and keeps the last read."""
        shown = path or PACKAGED
        try:
            if path is None:
                text = (
                    resources.files("ensemble.orchestrator.engine.flow")
                    .joinpath("workflow.toml")
                    .read_text(encoding="utf-8")
                )
            else:
                with open(path, encoding="utf-8") as f:
                    text = f.read()
            data = tomllib.loads(text)
        except (OSError, ValueError) as e:
            raise ValueError(f"{shown}: {e}") from None
        self._stages, self.path = _parse(data, shown), shown
        return self._stages

    def __getitem__(self, role: str) -> Stage:
        return self._read()[role]

    def __iter__(self) -> Iterator[str]:
        return iter(self._read())

    def __len__(self) -> int:
        return len(self._read())

    def _read(self) -> Mapping[str, Stage]:
        return self._stages if self._stages is not None else self.load()


def stage(table: Mapping, at: str = "stage") -> Stage:
    """table's Stage, unset keys defaulted; a ValueError names `at`, then the role."""
    if extra := sorted(set(table) - set(TYPES)):
        raise ValueError(f"{at}: unknown keys: {', '.join(extra)}")
    for key in ("role", "input"):
        if key not in table:
            raise ValueError(f"{at}: {key} is required")
    role = table["role"]
    if not isinstance(role, str) or not role:
        raise ValueError(f"{at}: role: want a non-empty str: {role!r}")
    at = f"stage {role}"
    for key, value in table.items():
        want = TYPES[key]
        if isinstance(want, tuple):
            if value not in want:
                raise ValueError(f"{at}: {key}: want one of {', '.join(want)}: {value!r}")
        elif type(value) is not want:
            raise ValueError(f"{at}: {key}: want a {want.__name__}: {value!r}")
    return Stage(**{**_defaults(role), **table})


def _defaults(role):
    return {
        "next": "",
        "require_instructions": True,
        "title_prefix": "",
        "retitle": False,
        "start": f"{role} started:",
        "done": f"{role} done:",
        "question": "Question:",
        "failed": f"{role} failed:",
        "first_failure": "review",
        "approve": False,
        "files": False,
        "hint": "",
    }


def _parse(data, shown):
    tables = data.get("stage")
    if (
        set(data) != {"stage"}
        or not isinstance(tables, list)
        or not tables
        or not all(isinstance(t, dict) for t in tables)
    ):
        raise ValueError(f"{shown}: want one or more [[stage]] tables and nothing else")
    stages = {}
    for n, table in enumerate(tables, 1):
        try:
            s = stage(table, f"stage {n}")
        except ValueError as e:
            raise ValueError(f"{shown}: {e}") from None
        if s.role in stages:
            raise ValueError(f"{shown}: stage {s.role}: listed twice")
        stages[s.role] = s
    _chains(stages, shown)
    return stages


def _chains(stages, shown):
    for role, s in stages.items():
        if s.next and (s.next == role or s.next not in stages):
            raise ValueError(f"{shown}: stage {role}: next {s.next!r} must name another stage")
    for role in stages:
        nxt, seen = stages[role].next, {role}
        while nxt:
            if nxt in seen:
                raise ValueError(f"{shown}: the next chain from {role!r} has a cycle")
            seen.add(nxt)
            nxt = stages[nxt].next


DEFAULT = Workflow()
