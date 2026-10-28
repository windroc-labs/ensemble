"""The `python3 -I -c` program of a `scripts/` installer shell function."""

from pathlib import Path

from tests.case import REPO


def program(function, script="install.sh"):
    text = Path(REPO, "scripts", script).read_text()
    body = text.split(f"{function}() {{\n", 1)[1].split("\n}\n", 1)[0]
    return body.split("python3 -I -c '\n", 1)[1].split("' ", 1)[0]
