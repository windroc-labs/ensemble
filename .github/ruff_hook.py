"""Pre-commit: ruff check --fix, then ruff format, on each staged .py file at CI's ruff version.

Works on the staged blob, so unstaged edits are neither committed nor lost; the working file is
rewritten only when it had none. Enabled by .githooks/pre-commit.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parent / "workflows" / "test.yml"
PIN = re.compile(r"\bruff==([0-9][\w.+-]*)")
REGULAR = {b"100644", b"100755"}


def say(message):
    print(f"pre-commit (ruff): {message}", file=sys.stderr)


def pinned_version(text):
    """CI's ruff pin, None unless every ruff== in the workflow names the same version."""
    versions = set(PIN.findall(text))
    return versions.pop() if len(versions) == 1 else None


def runner(version, tried=None):
    """The first ruff command at version; tried, when given, collects the rejected ones."""
    tried = [] if tried is None else tried
    for name, *args in (
        ["ruff"],
        ["uvx", f"ruff@{version}"],
        ["uv", "tool", "run", f"ruff@{version}"],
    ):
        path = shutil.which(name)
        if not path:
            continue
        probe = subprocess.run(
            [path, *args, "--version"], capture_output=True, text=True, check=False
        )
        if probe.returncode == 0 and probe.stdout.split()[-1:] == [version]:
            return [path, *args]
        tried.append(f"{' '.join([path, *args])}: {probe.stdout.strip() or probe.returncode}")
    return None


def git(*args, data=None):
    return subprocess.run(["git", *args], input=data, capture_output=True, check=True).stdout


def staged():
    names = git("diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR", "--", "*.py")
    if not names.strip(b"\0"):
        return []
    listed = git("--literal-pathspecs", "ls-files", "--stage", "-z", "--", *names.split(b"\0")[:-1])
    entries = []
    for line in listed.split(b"\0")[:-1]:
        info, path = line.split(b"\t", 1)
        mode, blob, stage = info.split(b" ")
        if mode in REGULAR and stage == b"0":
            entries.append((mode, blob, path))
    return entries


def fix(command, path, source):
    """Returns the fixed source and whether every problem was fixed."""
    clean = True
    for args, ok in ((("check", "--fix"), {0, 1}), (("format",), {0})):
        done = subprocess.run(
            [*command, *args, "--no-cache", "--force-exclude", b"--stdin-filename", path, "-"],
            input=source,
            capture_output=True,
            check=False,
        )
        if done.returncode:
            clean = False
            sys.stderr.buffer.write(done.stderr)
            sys.stderr.flush()
        if done.returncode in ok:
            source = done.stdout
    return source, clean


def main():
    entries = staged()
    if not entries:
        return 0
    version = pinned_version(WORKFLOW.read_text(encoding="utf-8")) if WORKFLOW.is_file() else None
    if not version:
        say(f"no single ruff== pin in {WORKFLOW}; skipped")
        return 0
    tried = []
    command = runner(version, tried)
    if not command:
        found = "; ".join(tried) or "neither ruff nor uv/uvx on PATH"
        say(f"no ruff {version} ({found}); skipped (install one; CI still checks)")
        return 0
    # `git commit <paths>` stages into this temporary index and the real one separately.
    only = Path(os.environ.get("GIT_INDEX_FILE", "")).name.startswith("next-index-")
    unstaged = set(git("diff", "--name-only", "-z").split(b"\0"))
    failed = fixed_any = False
    for mode, blob, path in entries:
        source = git("cat-file", "blob", blob.decode())
        fixed, clean = fix(command, path, source)
        failed = failed or not clean
        if fixed == source:
            continue
        fixed_any = True
        sha = git("hash-object", "-w", "--stdin", data=fixed).strip()
        git("update-index", "--cacheinfo", b"%s,%s,%s" % (mode, sha, path))
        name = path.decode(errors="replace")
        if path in unstaged:
            say(f"fixed the staged {name} only; its unstaged changes are left as they were")
        else:
            git("--literal-pathspecs", "checkout-index", "-f", "--", path)
            say(f"fixed {name}")
    if only and fixed_any:
        say("`git commit <paths>` cannot re-stage the fixes; run the same commit again")
        return 1
    if failed:
        say("ruff reported problems it cannot fix (above); commit aborted")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
