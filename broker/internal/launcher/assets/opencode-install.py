"""Installs the pinned OpenCode from its npm tarballs, each checked against its recorded sha512 before
anything is written: the opencode-ai package with its platform binary in place of the stub its
postinstall would replace, as npm and that postinstall leave it.
"""

import hashlib
import http.client
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

VERSION = "1.19.3"
REGISTRY = "https://registry.npmjs.org"
SHA512 = {  # package → its tarball's sha512, the npm dist.integrity in hex
    "opencode-ai": "c0345c52530389b68bcde7081af546500f676a085756acc259c504ecddea6a0a"
    "4688b494954e156e2c48f9ba6c12bc09a7e000b923b69614abfd7c4997b5dbad",
    "opencode-linux-arm64": "6351c8b0934afb3c5bb5de784c2ac423f1b79bf45200b09a29bcc069ac49605f"
    "991d01363c59479ae7aa5838a04284f5b5b88f8bff2d3cc8abc691a0717129ae",
    "opencode-linux-x64": "c33fa45e2ed992f2d6d38844228716dd1a04ce700b2776069ef36565d5c56ab6"
    "05d2fd63c83c80c293c6664c5be09643af0e28cb8f3f2c367cc021484034f187",
}
PLATFORMS = {"arm64": "opencode-linux-arm64", "amd64": "opencode-linux-x64"}
LIMIT = 512 << 20


def fail(message):
    raise SystemExit(f"opencode-install: {message}")


def fetch(name, opener=urllib.request.urlopen):
    url = f"{REGISTRY}/{name}/-/{name}-{VERSION}.tgz"
    try:
        with opener(url, timeout=300) as response:
            data = response.read(LIMIT + 1)
    except (OSError, http.client.HTTPException) as error:
        fail(f"downloading {name}@{VERSION} failed: {error}")
    if len(data) > LIMIT:
        fail(f"{name}@{VERSION} exceeds {LIMIT} bytes")
    if hashlib.sha512(data).hexdigest() != SHA512[name]:
        fail(f"{name}@{VERSION} does not match its recorded sha512")
    return data


def member(tarball, name, path):
    try:
        with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as archive:
            info = archive.getmember(path)
            stream = archive.extractfile(info) if info.isreg() else None
            if stream is None:
                fail(f"{name}: {path} is not a regular file")
            return stream.read()
    except (KeyError, tarfile.TarError, OSError):
        fail(f"{name}: no {path}")


def package(tarball, name):
    data = member(tarball, name, "package/package.json")
    try:
        manifest = json.loads(data)
    except ValueError:
        manifest = None
    if not isinstance(manifest, dict) or (manifest.get("name"), manifest.get("version")) != (
        name,
        VERSION,
    ):
        fail(f"{name}: package.json is not {name}@{VERSION}")
    return data


def check(binary):
    """Runs binary --version from a scratch directory, also its HOME, that is removed afterwards."""
    scratch = Path(tempfile.mkdtemp(prefix="opencode-check-"))
    try:
        executable = scratch / "opencode"
        executable.write_bytes(binary)
        executable.chmod(0o755)
        try:
            run = subprocess.run(
                [str(executable), "--version"],
                env={"HOME": str(scratch), "PATH": os.environ.get("PATH", os.defpath)},
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            reported = run.stdout.strip()
        except (OSError, subprocess.SubprocessError) as error:
            reported = str(error)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if reported != VERSION:
        fail(f"opencode --version gave {reported[:200]!r}, want {VERSION}")


def install(arch, prefix, fetch=fetch):
    platform = PLATFORMS.get(arch)
    if platform is None:
        fail(f"no OpenCode package for architecture {arch!r}")
    wrapper, native = fetch("opencode-ai"), fetch(platform)
    files = {
        "package.json": package(wrapper, "opencode-ai"),
        "LICENSE": member(wrapper, "opencode-ai", "package/LICENSE"),
    }
    package(native, platform)
    binary = member(native, platform, "package/bin/opencode")
    check(binary)
    root = prefix / "lib/node_modules/opencode-ai"
    (root / "bin").mkdir(parents=True)
    for file, data in files.items():
        (root / file).write_bytes(data)
    executable = root / "bin/opencode.exe"
    executable.write_bytes(binary)
    executable.chmod(0o755)
    (prefix / "bin").mkdir(exist_ok=True)
    (prefix / "bin/opencode").symlink_to("../lib/node_modules/opencode-ai/bin/opencode.exe")


def main(argv):
    if len(argv) != 2 or not os.path.isabs(argv[1]):
        fail("usage: opencode-install.py ARCH /ABSOLUTE/PREFIX")
    install(argv[0], Path(argv[1]))


if __name__ == "__main__":
    main(sys.argv[1:])
