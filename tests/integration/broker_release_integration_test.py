import hashlib
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

from tests.case import REPO, Case

TOOLS = (
    "sh",
    "dirname",
    "mkdir",
    "mktemp",
    "cp",
    "rm",
    "cat",
    "grep",
    "sed",
    "tar",
    "gzip",
    "shasum",
    "basename",
)
ASSET = "ensemble-broker-darwin-arm64.tar.gz"
COMMIT = "a" * 40
FAKE = """#!/bin/sh
printf '%s\\n' "$(basename "$0") $*" >> "$LOG"
case "$(basename "$0") $*" in
  "uname -sm") echo "$PLATFORM" ;;
  "git -C "*" fetch "*) [ -z "${NO_TAG:-}" ] || exit 1 ;;
  "git -C "*" rev-parse "*) echo "$COMMIT" ;;
  "git -C "*" show "*) cat "$MANIFEST" ;;
  "git -C "*" archive "*) tar -cf "$5" -C "$SOURCE" . ;;
  "gh release view"*) [ -z "${NO_RELEASE:-}" ] || { echo "release not found" >&2; exit 1; } ;;
  "gh release upload"*) cp "$4" "$5" "$UPLOADED" ;;
  "go build"*)
    [ "$GOOS/$GOARCH" = darwin/arm64 ] || exit 1
    while [ "$#" -gt 0 ]; do [ "$1" = -o ] && echo built > "$2"; shift; done ;;
  "codesign -dv "*) echo "CodeDirectory v=20500 size=1 flags=0x10002(adhoc,runtime) hashes=1" >&2 ;;
esac
"""


class Release(Case):
    def setUp(self):
        self.bin = Path(self.home, "bin")
        self.bin.mkdir()
        for tool in TOOLS:
            (self.bin / tool).symlink_to(shutil.which(tool))
        for tool in ("uname", "git", "gh", "go", "codesign"):
            (self.bin / tool).write_text(FAKE)
            (self.bin / tool).chmod(0o755)
        self.repo = Path(self.home, "repo")
        (self.repo / "scripts").mkdir(parents=True)
        shutil.copy(Path(REPO, "scripts/release-broker.sh"), self.repo / "scripts")
        self.source = Path(self.home, "source")
        (self.source / "src/broker").mkdir(parents=True)
        for name in ("build.sh", "with-go-state.sh"):
            shutil.copy(Path(REPO, "src/broker", name), self.source / "src/broker")
        self.manifest = Path(self.home, "plugin.json")
        self.manifest.write_text(json.dumps({"name": "ensemble", "version": "9.8.7"}, indent=2))
        self.uploaded = Path(self.home, "uploaded")
        self.uploaded.mkdir()
        self.log = Path(self.home, "calls.log")
        self.env = {
            "HOME": self.home,
            "PATH": str(self.bin),
            "LOG": str(self.log),
            "PLATFORM": "Darwin arm64",
            "COMMIT": COMMIT,
            "MANIFEST": str(self.manifest),
            "SOURCE": str(self.source),
            "UPLOADED": str(self.uploaded),
        }

    def release(self, *args, **env):
        return subprocess.run(
            [str(self.bin / "sh"), str(self.repo / "scripts/release-broker.sh"), *args],
            env={**self.env, **env},
            capture_output=True,
            text=True,
            timeout=60,
        )

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def assert_nothing_built_or_uploaded(self, result):
        self.assertEqual(result.returncode, 1)
        self.assertFalse([c for c in self.calls() if c.startswith(("go ", "gh release upload"))])
        self.assertEqual(list(self.uploaded.iterdir()), [])

    def test_builds_the_tag_and_uploads_the_asset_with_its_sha256(self):
        result = self.release("v9.8.7")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        repo = self.repo.resolve()
        self.assertIn(
            f"git -C {repo} fetch --quiet --no-tags https://github.com/windroc-labs/ensemble.git"
            " refs/tags/v9.8.7",
            calls,
        )
        self.assertIn(f"git -C {repo} show {COMMIT}:.claude-plugin/plugin.json", calls)
        self.assertIn("gh release view v9.8.7 --repo windroc-labs/ensemble", calls)
        [archive] = [c for c in calls if " archive " in c]
        self.assertTrue(archive.endswith(f" {COMMIT}"), archive)
        [upload] = [c for c in calls if c.startswith("gh release upload")]
        work = upload.split()[4].rsplit("/", 1)[0]
        self.assertEqual(
            upload,
            f"gh release upload v9.8.7 {work}/{ASSET} {work}/{ASSET}.sha256"
            " --repo windroc-labs/ensemble --clobber",
        )
        self.assertLess(calls.index(archive), calls.index(upload))
        asset = (self.uploaded / ASSET).read_bytes()
        digest = hashlib.sha256(asset).hexdigest()
        self.assertEqual((self.uploaded / f"{ASSET}.sha256").read_text(), f"{digest}  {ASSET}\n")
        with tarfile.open(self.uploaded / ASSET) as archive_file:
            self.assertEqual(archive_file.getnames(), ["ensemble-broker"])
            member = archive_file.extractfile("ensemble-broker")
            assert member
            self.assertEqual(member.read(), b"built\n")
        self.assertIn(f"sha256 {digest}", result.stdout)
        self.assertFalse(Path(work).exists())

    def test_tag_other_than_the_plugin_version_stops_before_building(self):
        result = self.release("v9.8.6")
        self.assert_nothing_built_or_uploaded(result)
        self.assertIn("tag v9.8.6 is not v9.8.7", result.stderr)

    def test_unpublished_release_stops_before_building(self):
        result = self.release("v9.8.7", NO_RELEASE="1")
        self.assert_nothing_built_or_uploaded(result)
        self.assertIn("no published Release v9.8.7", result.stderr)

    def test_missing_tag_stops_before_building(self):
        result = self.release("v9.8.7", NO_TAG="1")
        self.assert_nothing_built_or_uploaded(result)
        self.assertIn("could not fetch tag v9.8.7", result.stderr)

    def test_other_platforms_stop_before_fetching(self):
        for platform in ("Darwin x86_64", "Linux x86_64"):
            result = self.release("v9.8.7", PLATFORM=platform)
            self.assert_nothing_built_or_uploaded(result)
            self.assertIn(f"not {platform}", result.stderr)
        self.assertFalse([c for c in self.calls() if c.startswith("git ")])

    def test_anything_but_one_version_tag_is_a_usage_error(self):
        for args in ([], ["9.8.7"], ["v9.8.7", "v9.8.7"], ["main"]):
            result = self.release(*args)
            self.assertEqual(result.returncode, 2, args)
            self.assertIn("usage: sh release-broker.sh v<X.Y.Z>", result.stderr)
        self.assertEqual(self.calls(), [])
