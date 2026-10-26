"""Installs OpenCode at the requested version from its npm tarballs, each checked against the sha512
of the registry's metadata for that version before anything is written: the opencode-ai package with
its platform binary in place of the stub its postinstall would replace, as npm and that postinstall
leave it.
"""

import base64
import binascii
import hashlib
import http.client
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

REGISTRY = "https://registry.npmjs.org"
PLATFORMS = {"arm64": "opencode-linux-arm64", "amd64": "opencode-linux-x64"}
LIMIT = 512 << 20
METADATA_LIMIT = 1 << 20
INTEGRITY = re.compile(r"sha512-([A-Za-z0-9+/]+={0,2})")


def fail(message):
    raise SystemExit(f"opencode-install: {message}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, *_):
        fail(f"{req.full_url} answered a redirect")


def open_url(url, timeout):
    return urllib.request.build_opener(NoRedirect).open(url, timeout=timeout)


def download(url, limit, what, opener):
    try:
        with opener(url, timeout=300) as response:
            data = response.read(limit + 1)
    except (OSError, http.client.HTTPException) as error:
        fail(f"downloading {what} failed: {error}")
    if len(data) > limit:
        fail(f"{what} exceeds {limit} bytes")
    return data


def integrity(name, version, opener):
    """The sha512 the registry's metadata for name@version records: its one dist.integrity entry."""
    data = download(
        f"{REGISTRY}/{name}/{version}", METADATA_LIMIT, f"{name}@{version} metadata", opener
    )
    try:
        metadata = json.loads(data)
    except ValueError:
        metadata = None
    if not isinstance(metadata, dict) or (metadata.get("name"), metadata.get("version")) != (
        name,
        version,
    ):
        fail(f"{name}@{version}: registry metadata is not for {name}@{version}")
    dist = metadata.get("dist")
    entry = dist.get("integrity") if isinstance(dist, dict) else None
    match = INTEGRITY.fullmatch(entry) if isinstance(entry, str) else None
    try:
        digest = base64.b64decode(match[1], validate=True) if match else b""
    except binascii.Error:
        digest = b""
    if len(digest) != hashlib.sha512().digest_size:
        fail(f"{name}@{version}: registry integrity is not one sha512 entry")
    return digest


def fetch(name, version, opener=open_url):
    digest = integrity(name, version, opener)
    data = download(f"{REGISTRY}/{name}/-/{name}-{version}.tgz", LIMIT, f"{name}@{version}", opener)
    if hashlib.sha512(data).digest() != digest:
        fail(f"{name}@{version} does not match its registry sha512")
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


def package(tarball, name, version):
    data = member(tarball, name, "package/package.json")
    try:
        manifest = json.loads(data)
    except ValueError:
        manifest = None
    if not isinstance(manifest, dict) or (manifest.get("name"), manifest.get("version")) != (
        name,
        version,
    ):
        fail(f"{name}: package.json is not {name}@{version}")
    return data


def check(binary, version):
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
    if reported != version:
        fail(f"opencode --version gave {reported[:200]!r}, want {version}")


def install(arch, prefix, version, fetch=fetch):
    if not re.fullmatch(r"\d+\.\d+\.\d+", version, re.ASCII):
        fail(f"invalid version {version[:40]!r}")
    platform = PLATFORMS.get(arch)
    if platform is None:
        fail(f"no OpenCode package for architecture {arch!r}")
    wrapper, native = fetch("opencode-ai", version), fetch(platform, version)
    files = {
        "package.json": package(wrapper, "opencode-ai", version),
        "LICENSE": member(wrapper, "opencode-ai", "package/LICENSE"),
    }
    package(native, platform, version)
    binary = member(native, platform, "package/bin/opencode")
    check(binary, version)
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
    if len(argv) != 3 or not os.path.isabs(argv[1]):
        fail("usage: opencode-install.py ARCH /ABSOLUTE/PREFIX VERSION")
    install(argv[0], Path(argv[1]), argv[2])


if __name__ == "__main__":
    main(sys.argv[1:])
