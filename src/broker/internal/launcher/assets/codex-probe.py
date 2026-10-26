import os
import subprocess
import sys
from pathlib import Path


def probe(path, version):
    path = native = Path(path)
    candidates = list(path.parent.parent.glob("lib/node_modules/@openai/**/vendor/*/bin/codex"))
    if candidates:
        if len(candidates) != 1:
            raise SystemExit("ambiguous image Codex runtime")
        native = candidates[0]
    companion = native.with_name("codex-code-mode-host")
    if not companion.is_file() or not os.access(companion, os.X_OK):
        raise SystemExit("Codex code-mode host missing or not executable")
    result = subprocess.run([str(path), "--version"], check=True, capture_output=True, timeout=15)
    if result.stdout.strip() != ("codex-cli " + version).encode():
        raise SystemExit("Codex version mismatch")
    subprocess.run([str(companion), "--help"], check=True, capture_output=True, timeout=15)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Codex probe requires path and version")
    probe(sys.argv[1], sys.argv[2])
