"""Merges a Docker config, TOML on stdin, under the file of that name the image's bootstrap wrote in
a container's own client home: `toml_merge.py HOME NAME`. Each key the bootstrap's file sets wins;
the result replaces it as a private regular file. Exit 2: bad arguments or TOML.
"""

import datetime
import json
import math
import os
import re
import sys
import tempfile
import tomllib

BARE = re.compile(r"[A-Za-z0-9_-]+")


def merge(base: dict, over: dict) -> dict:
    """`base` with `over` written on top, tables merged key by key."""
    merged = dict(base)
    for key, value in over.items():
        found = merged.get(key)
        merged[key] = (
            merge(found, value) if isinstance(found, dict) and isinstance(value, dict) else value
        )
    return merged


def _string(text: str) -> str:
    return json.dumps(text, ensure_ascii=False).replace("\x7f", "\\u007f")


def _key(key: str) -> str:
    return key if BARE.fullmatch(key) else _string(key)


def _value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return repr(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, list):
        return f"[{', '.join(map(_value, value))}]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{_key(k)} = {_value(v)}" for k, v in value.items()) + "}"
    raise TypeError(f"not a TOML value: {type(value).__name__}")


def _table(table: dict, path: tuple[str, ...], out: list[str]) -> None:
    tables = {k: v for k, v in table.items() if isinstance(v, dict)}
    if path and (len(tables) < len(table) or not tables):
        out.append(f"[{'.'.join(map(_key, path))}]")
    out.extend(f"{_key(k)} = {_value(v)}" for k, v in table.items() if k not in tables)
    for key, value in tables.items():
        _table(value, (*path, key), out)


def dumps(table: dict) -> str:
    """TOML that tomllib reads back as `table`: sub-tables as headers, tables in arrays inline."""
    out: list[str] = []
    _table(table, (), out)
    return "".join(f"{line}\n" for line in out)


def _bootstrap(path: str) -> dict:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}
    with os.fdopen(fd, "rb") as stream:
        return tomllib.load(stream)


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not os.path.isabs(argv[0]) or not argv[1].endswith(".toml"):
        print("usage: toml_merge.py HOME NAME.toml", file=sys.stderr)
        return 2
    home, name = argv
    if os.path.basename(name) != name or name.startswith("."):
        print(f"toml_merge: {name!r}: want a plain file name", file=sys.stderr)
        return 2
    path = os.path.join(home, name)
    try:
        text = dumps(merge(tomllib.loads(sys.stdin.buffer.read().decode()), _bootstrap(path)))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        print(f"toml_merge: invalid TOML: {error}", file=sys.stderr)
        return 2
    fd, staged = tempfile.mkstemp(dir=home, prefix=f".{name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(staged, path)
    except BaseException:
        os.unlink(staged)
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
