import gzip
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

ROOT = Path("/opt/ensemble/runtime")
LIMIT = 512 << 20
REQUIRED = ("codex", "codex-code-mode-host")


def safe(path, *, directory=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise SystemExit("Codex runtime path missing") from None
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise SystemExit("unsafe Codex runtime path")
    return info


def validate(stage):
    safe(stage, directory=True)
    size = 0
    for path in stage.rglob("*"):
        info = safe(path, directory=path.is_dir())
        size += info.st_size
        if size > LIMIT:
            raise SystemExit("Codex runtime exceeds limit")
    for name in REQUIRED:
        info = safe(stage / "bin" / name)
        if not info.st_size or info.st_mode & 0o111 != 0o111:
            raise SystemExit("Codex runtime executable missing")


def stage_archive(stream, stage):
    stage.mkdir(mode=0o755)
    with tempfile.TemporaryFile() as expanded:
        with gzip.GzipFile(fileobj=stream) as archive:
            size = 0
            while chunk := archive.read(1 << 20):
                size += len(chunk)
                if size > LIMIT:
                    raise SystemExit("Codex expanded archive exceeds limit")
                expanded.write(chunk)
        expanded.seek(0)
        with tarfile.open(fileobj=expanded, mode="r:") as archive:
            seen, entries = set(), []
            for entry in archive:
                name = entry.name.rstrip("/")
                if (
                    len(seen) >= 4096
                    or name in seen
                    or PurePosixPath(name).as_posix() != name
                    or ".." in PurePosixPath(name).parts
                    or not (name == "package" or name.startswith("package/"))
                    or "\\" in name
                    or not (entry.isfile() or entry.isdir())
                    or entry.mode & 0o7022
                    or entry.size < 0
                    or (entry.isdir() and entry.size)
                ):
                    raise SystemExit("unsafe Codex archive entry")
                seen.add(name)
                entries.append(entry)
            prefixes = [
                name.removesuffix("bin/codex")
                for name in seen
                if name.startswith("package/vendor/") and name.endswith("/bin/codex")
            ]
            if len(prefixes) != 1:
                raise SystemExit("Codex runtime target missing or ambiguous")
            prefix = prefixes[0]
            for entry in entries:
                if not entry.name.startswith(prefix):
                    continue
                relative = entry.name[len(prefix) :].rstrip("/")
                if not relative:
                    continue
                destination = stage / relative
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                if entry.isdir():
                    destination.mkdir(exist_ok=True, mode=0o755)
                else:
                    with archive.extractfile(entry) as source, destination.open("xb") as output:
                        shutil.copyfileobj(source, output)
                        os.fchmod(output.fileno(), 0o755 if entry.mode & 0o111 else 0o644)
    validate(stage)


def probe(path, version):
    path = Path(path)
    native = path
    if path == ROOT.parent / "bin/codex":
        native = ROOT / "codex/bin/codex"
        if not native.exists():
            native = ROOT.parent / "clients/bin/codex"
    candidates = list(native.parent.parent.glob("lib/node_modules/@openai/**/vendor/*/bin/codex"))
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


def update(action, stream, root=ROOT):
    safe(root, directory=True)
    stage, active = root / "codex.next", root / "codex"
    if action == "reset":
        for path in (stage, active):
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.exists():
                safe(path, directory=True)
                shutil.rmtree(path)
    elif action == "publish":
        validate(stage)
        os.replace(stage, active)
    elif action == "stage":
        try:
            stage_archive(stream, stage)
        except (EOFError, tarfile.TarError, gzip.BadGzipFile):
            raise SystemExit("invalid Codex runtime archive") from None
    else:
        raise SystemExit("invalid Codex update action")


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "probe":
        probe(sys.argv[2], sys.argv[3])
    elif os.geteuid() == 0 and len(sys.argv) == 2:
        update(sys.argv[1], sys.stdin.buffer)
    else:
        raise SystemExit(
            "Codex update requires root and one action; probe requires path and version"
        )
