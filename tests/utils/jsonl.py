"""Read a whole JSONL file; a blank or partial line raises."""

import json


def read_jsonl(path, *, missing_ok=False):
    """Each line of `path` parsed; a missing file is [] when `missing_ok`, else it raises."""
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f]
    except FileNotFoundError:
        if missing_ok:
            return []
        raise
