"""Ordered config resources with per-layer local sections.

Tables merge key by key; higher values and whole lists replace lower values. Each layer's local
section overlays its file before the next layer. A section is its package's final name, - as _.
Only supplied layers are read and validated, against their own and lower files, plus the local
[utils] section. Unknown top-level keys are rejected, a renamed or moved one naming its new place
(RENAMED_KEYS, MOVED_KEYS), inside tolerate_unknown_keys reported and dropped; nested tables are
validated by their owners. Flat local keys are rejected.
Explicit paths replace files; the defaults stay their validation schema.
Opaque paths preserve values and replace them wholesale; * matches one key.
"""

import contextlib
import copy
import os
import tomllib
from collections.abc import Callable, Iterable, Iterator, Mapping
from importlib.resources import files
from types import MappingProxyType
from typing import Any

from ensemble.utils.paths import STATE

LOCAL = f"{STATE}/config.local.toml"
RENAMED_KEYS = MappingProxyType({"runtime": "client"})
MOVED_KEYS = MappingProxyType({"accounts": "[session.clients.<client>] account"})
_TOLERATED: list[Callable[[str], None]] = []


class ConfigFileError(ValueError):
    pass


def renamed_key(key: str) -> str:
    """An unknown-key error's hint, else ""."""
    if key in RENAMED_KEYS:
        return f"; renamed to {RENAMED_KEYS[key]!r}"
    return f"; moved to {MOVED_KEYS[key]}" if key in MOVED_KEYS else ""


@contextlib.contextmanager
def tolerate_unknown_keys(report: Callable[[str], None]) -> Iterator[None]:
    _TOLERATED.append(report)
    try:
        yield
    finally:
        _TOLERATED.pop()


def merge(base: dict, over: dict, *, opaque: tuple[tuple[str, ...], ...] = ()) -> dict:
    out = dict(base)
    for k, v in over.items():
        nested = tuple(path[1:] for path in opaque if path and path[0] in (k, "*"))
        out[k] = (
            merge(out[k], v, opaque=nested)
            if () not in nested and isinstance(v, dict) and isinstance(out.get(k), dict)
            else v
        )
    return out


def loads(text: str) -> dict:
    return tomllib.loads(text)


def load(path: str) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


class _Array(tuple):
    """A frozen list; thawed, a list again."""


def freeze(value: Any) -> Any:
    """A read-only deep copy: lists as tuples."""
    if isinstance(value, Mapping):
        return MappingProxyType({k: freeze(v) for k, v in value.items()})
    if isinstance(value, (list, _Array)):
        return _Array(freeze(v) for v in value)
    if isinstance(value, tuple):
        return tuple(freeze(v) for v in value)
    return copy.deepcopy(value)


def thaw(value: Any) -> Any:
    """freeze's inverse, a fresh copy."""
    if isinstance(value, Mapping):
        return {k: thaw(v) for k, v in value.items()}
    if isinstance(value, _Array):
        return [thaw(v) for v in value]
    if isinstance(value, tuple):
        return tuple(thaw(v) for v in value)
    return copy.deepcopy(value)


def read_resources(
    resources: Iterable[tuple[str, str]],
    *,
    local: str = LOCAL,
    paths: Mapping[str, str] | None = None,
    validate: Callable[[str, str, Mapping], None] | None = None,
    opaque: tuple[tuple[str, ...], ...] = (),
) -> dict:
    resources = tuple(resources)
    configs, known = {}, {}
    for package, filename in resources:
        section = package.rsplit(".", 1)[-1].replace("-", "_")
        if section in configs:
            raise ConfigFileError(f"duplicate config layer {section!r}")
        resource = files(package).joinpath(filename)
        with resource.open("rb") as stream:
            default = tomllib.load(stream)
        override = paths.get(package) if paths is not None else None
        configs[section] = load(override) if override is not None else default
        if validate is not None:
            validate(package, override if override is not None else str(resource), configs[section])
        known[section] = (default, configs[section])
    if paths and (extra := paths.keys() - {p for p, _ in resources}):
        raise ConfigFileError(f"config path override has no resource: {min(extra)!r}")
    configs, known = {"utils": {}, **configs}, {"utils": ({}, {}), **known}
    found = _load(os.path.expanduser(local))
    for section in configs:
        if section in found and not isinstance(found[section], dict):
            raise ConfigFileError(f"{local}: [{section}]: want a table")
    keys = {key for tables in known.values() for table in tables for key in table}
    if flat := sorted(
        key for key, value in found.items() if not isinstance(value, dict) or key in keys
    ):
        key = flat[0]
        path, _ = next(_leaves({key: found[key]}, ()))
        schemas = {p: merge(*tables) for p, tables in known.items() if p != "utils" and any(tables)}
        targets = _flat_sections(path, schemas)
        sections = ", ".join(f"[{p}]" for p in targets)
        hint = (
            f"move it under {sections}"
            if len(targets) == 1
            else f"move it under one of {sections}"
            if targets
            else "no supplied layer has it; move it under its owning layer's section"
        )
        raise ConfigFileError(
            f"{local}: unknown key {'.'.join(path)!r}: flat local configuration; {hint}"
        )
    schema, out = {}, {}
    for section, config in configs.items():
        schema = merge(schema, merge(*known[section]))
        overlay = found.get(section, {})
        checked = {}
        for keys, value in _leaves(overlay, (), opaque=opaque):
            if keys[0] not in schema:
                error = f"{local}: unknown key {keys[0]!r} in [{section}]{renamed_key(keys[0])}"
                if not _TOLERATED:
                    raise ConfigFileError(error)
                _TOLERATED[-1](error)
                continue
            if value == {} and not _opaque(keys, opaque):
                continue
            table = checked
            for key in keys[:-1]:
                table = table.setdefault(key, {})
            table[keys[-1]] = value
        out = merge(out, merge(config, checked, opaque=opaque), opaque=opaque)
    return out


def _flat_sections(keys: tuple, schemas: Mapping[str, dict]) -> list[str]:
    having = [p for p, table in schemas.items() if keys[0] in table]
    exact = [p for p in having if _has(schemas[p], keys)]
    shaped = [
        p
        for p in having
        if any(
            len(k) == len(keys) and k[0] == keys[0] and k[-1] == keys[-1]
            for k, _ in _leaves(schemas[p], ())
        )
    ]
    suffix = [p for p in having if any(_has(schemas[p], keys[i:]) for i in range(1, len(keys)))]
    return exact or shaped or suffix or having


def unset(cfg: Mapping, keys: tuple[str, ...]) -> list[str]:
    """Dotted `keys` (`*`: each subtable) missing or "" in `cfg`."""
    return [k for key in keys for k, v in _values(cfg, key.split("."), []) if v in (None, "")]


def _load(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def _has(table, keys) -> bool:
    for k in keys:
        if not isinstance(table, Mapping) or k not in table:
            return False
        table = table[k]
    return True


def _opaque(keys: tuple, paths: tuple[tuple[str, ...], ...]) -> bool:
    return any(
        len(keys) == len(path)
        and all(want in (key, "*") for key, want in zip(keys, path, strict=True))
        for path in paths
    )


def _leaves(
    table: Mapping, at: tuple, *, opaque: tuple[tuple[str, ...], ...] = ()
) -> Iterator[tuple[tuple, object]]:
    if not table and at:
        yield at, {}
    for k, v in table.items():
        if isinstance(v, Mapping) and not _opaque((*at, k), opaque):
            yield from _leaves(v, (*at, k), opaque=opaque)
        else:
            yield (*at, k), v


def _values(cfg, keys: list[str], at: list[str]) -> Iterator[tuple[str, object]]:
    if not keys:
        yield ".".join(at), cfg
        return
    head, rest = keys[0], keys[1:]
    if head == "*":
        for k, v in cfg.items() if isinstance(cfg, Mapping) else ():
            yield from _values(v, rest, [*at, k])
    elif isinstance(cfg, Mapping) and head in cfg:
        yield from _values(cfg[head], rest, [*at, head])
    elif "*" not in keys:
        yield ".".join([*at, *keys]), None
