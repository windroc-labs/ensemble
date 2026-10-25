import io
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

from tests.case import REPO, Case

BROKER = Path(REPO, "broker")
TOOLS = (
    "sh",
    "dirname",
    "basename",
    "mkdir",
    "cp",
    "mktemp",
    "rm",
    "cat",
    "grep",
    "sed",
    "tar",
    "gzip",
)
ASSET = "ensemble-broker-darwin-arm64.tar.gz"
TARGET = "/usr/local/libexec/ensemble"
VERIFY = (
    "--repo windroc-labs/ensemble --signer-workflow windroc-labs/ensemble/.github/workflows/release.yml"
    " --source-ref refs/tags/v9.8.7 --deny-self-hosted-runners"
)
SIGN = (
    "codesign --force --sign - --options runtime --identifier com.github.windroc.ensemble-broker "
)
FAKE = """#!/bin/sh
printf '%s\\n' "$(basename "$0") $*" >> "$LOG"
case "$(basename "$0") $*" in
  "go build"*) while [ "$#" -gt 0 ]; do [ "$1" = -o ] && : > "$2"; shift; done ;;
  "codesign -dv "*) echo "CodeDirectory v=20500 size=1 $FLAGS hashes=1" >&2 ;;
  "codesign -d --entitlements"*) echo "$ENTITLEMENTS" ;;
  "sudo install -d"*) [ ! -e "$HOME/.ensemble/docker/worker/Dockerfile" ] || : > "$SEEDED" ;;
  "sudo install -o"*) cp "$8" "$INSTALLED" ;;
  "gh release download"*)
    [ -n "${RELEASED:-}" ] || { echo "release not found" >&2; exit 1; }
    cp "$RELEASED" "$9/$7" ;;
  "gh attestation verify"*)
    signer=windroc-labs/ensemble/.github/workflows/release.yml
    want="attestation verify $3 --repo windroc-labs/ensemble --signer-workflow $signer"
    [ "$*" = "$want --source-ref ${ATTESTED_REF:-none} --deny-self-hosted-runners" ] || exit 1 ;;
esac
"""


class Install(Case):
    def setUp(self):
        self.bin = Path(self.home, "bin")
        self.bin.mkdir()
        for tool in TOOLS:
            (self.bin / tool).symlink_to(shutil.which(tool))
        (self.bin / "uname").write_text('#!/bin/sh\necho "$PLATFORM"\n')
        (self.bin / "uname").chmod(0o755)
        self.log = Path(self.home, "calls.log")
        self.plugin = Path(self.home, "plugin")
        (self.plugin / "broker/worker").mkdir(parents=True)
        for name in ("install.sh", "build.sh", "with-go-state.sh", "worker/Dockerfile"):
            shutil.copy(BROKER / name, self.plugin / "broker" / name)
        (self.plugin / ".claude-plugin").mkdir()
        self.manifest = self.plugin / ".claude-plugin/plugin.json"
        self.manifest.write_text(json.dumps({"name": "ensemble", "version": "9.8.7"}, indent=2))
        self.asset = Path(self.home, "asset.tar.gz")
        with tarfile.open(self.asset, "w:gz") as archive:
            member = tarfile.TarInfo("ensemble-broker")
            member.size = len(b"released binary")
            archive.addfile(member, io.BytesIO(b"released binary"))
        self.installed = Path(self.home, "installed")
        self.seeded = Path(self.home, "seeded-before-sudo")
        self.env = {
            "HOME": self.home,
            "PATH": str(self.bin),
            "LOG": str(self.log),
            "FLAGS": "flags=0x10002(adhoc,runtime)",
            "ENTITLEMENTS": "Executable=/x",
            "INSTALLED": str(self.installed),
            "SEEDED": str(self.seeded),
            "PLATFORM": "Darwin arm64",
        }

    def fake(self, *names):
        for name in names:
            path = self.bin / name
            path.write_text(FAKE)
            path.chmod(0o755)

    def run_script(self, name, *args, **env):
        return subprocess.run(
            [str(self.bin / "sh"), str(self.plugin / "broker" / name), *args],
            env={**self.env, **env},
            capture_output=True,
            text=True,
            timeout=60,
        )

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_build_script_builds_and_signs(self):
        self.fake("go", "codesign")
        out = Path(self.home, "out/ensemble-broker")
        out.parent.mkdir()
        result = self.run_script("build.sh", str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.calls(),
            [
                f"go build -trimpath -o {out} .",
                f"{SIGN}{out}",
                f"codesign --verify --strict {out}",
                f"codesign -dv {out}",
                f"codesign -d --entitlements - {out}",
            ],
        )

    def test_build_script_rejects_a_binary_without_the_hardened_runtime(self):
        self.fake("go", "codesign")
        result = self.run_script("build.sh", str(Path(self.home, "b")), FLAGS="flags=0x2(adhoc)")
        self.assertEqual(result.returncode, 1)
        self.assertIn("lacks the hardened runtime", result.stderr)

    def test_build_script_rejects_get_task_allow(self):
        self.fake("go", "codesign")
        result = self.run_script(
            "build.sh", str(Path(self.home, "b")), ENTITLEMENTS="com.apple.security.get-task-allow"
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("get-task-allow", result.stderr)

    def test_build_script_without_one_output_is_a_usage_error(self):
        self.fake("go", "codesign")
        for args in ([], ["a", "b"]):
            result = self.run_script("build.sh", *args)
            self.assertEqual(result.returncode, 2, args)
            self.assertIn("build.sh: usage: sh build.sh <output>", result.stderr)
        self.assertEqual(self.calls(), [])

    def assert_installed(self, calls, source):
        self.assertEqual(calls[0], f"sudo install -d -o root -g wheel -m 0755 {TARGET}")
        self.assertEqual(
            calls[1], f"sudo install -o root -g wheel -m 0755 {source} {TARGET}/ensemble-broker"
        )
        self.assertEqual(len(calls), 2)

    def assert_nothing_installed(self, result):
        self.assertEqual(result.returncode, 1)
        self.assertFalse([c for c in self.calls() if c.startswith("sudo")])
        self.assertFalse(self.installed.exists())
        self.assertFalse(Path(self.ensemble, "docker").exists())

    def test_downloads_verifies_and_installs_the_release_asset(self):
        self.fake("gh", "sudo")
        result = self.run_script(
            "install.sh", RELEASED=str(self.asset), ATTESTED_REF="refs/tags/v9.8.7"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        work = calls[0].rsplit(" ", 1)[1]
        self.assertEqual(
            calls[0],
            f"gh release download v9.8.7 --repo windroc-labs/ensemble --pattern {ASSET} --dir {work}",
        )
        self.assertEqual(calls[1], f"gh attestation verify {work}/{ASSET} {VERIFY}")
        self.assert_installed(calls[2:], f"{work}/ensemble-broker")
        self.assertEqual(self.installed.read_bytes(), b"released binary")
        seeded = Path(self.ensemble, "docker/worker/Dockerfile")
        self.assertEqual(seeded.read_text(), (BROKER / "worker/Dockerfile").read_text())
        self.assertTrue(self.seeded.exists())
        self.assertFalse(os.path.exists(work))

    def test_missing_asset_fails_naming_retry_and_build(self):
        self.fake("gh", "sudo")
        result = self.run_script("install.sh", ATTESTED_REF="refs/tags/v9.8.7")
        self.assert_nothing_installed(result)
        self.assertEqual(len(self.calls()), 1)
        self.assertIn(f"could not download {ASSET} from release v9.8.7", result.stderr)
        self.assertIn("retry in a few minutes", result.stderr)
        self.assertIn(f"sh {self.plugin.resolve()}/broker/install.sh --build", result.stderr)

    def test_local_asset_is_verified_then_installed(self):
        self.fake("gh", "sudo")
        result = self.run_script(
            "install.sh", ENSEMBLE_BROKER_ASSET=str(self.asset), ATTESTED_REF="refs/tags/v9.8.7"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertRegex(calls[0], rf"^gh attestation verify (/\S+)/{ASSET} {VERIFY}$")
        work = calls[0].split()[3].rsplit("/", 1)[0]
        self.assertNotEqual(work, self.home)
        self.assert_installed(calls[1:], f"{work}/ensemble-broker")
        self.assertEqual(self.installed.read_bytes(), b"released binary")

    def test_unattested_asset_aborts_before_install(self):
        self.fake("gh", "sudo")
        result = self.run_script("install.sh", ENSEMBLE_BROKER_ASSET=str(self.asset))
        self.assert_nothing_installed(result)
        self.assertIn("failed provenance verification for v9.8.7; nothing installed", result.stderr)

    def test_asset_attested_for_another_tag_aborts(self):
        self.fake("gh", "sudo")
        result = self.run_script(
            "install.sh", ENSEMBLE_BROKER_ASSET=str(self.asset), ATTESTED_REF="refs/tags/v9.8.6"
        )
        self.assert_nothing_installed(result)
        self.assertIn("failed provenance verification for v9.8.7", result.stderr)

    def test_missing_gh_fails_naming_build(self):
        self.fake("sudo")
        result = self.run_script("install.sh", RELEASED=str(self.asset))
        self.assert_nothing_installed(result)
        self.assertIn("gh is required", result.stderr)
        self.assertIn("install.sh --build", result.stderr)

    def test_unknown_argument_is_a_usage_error(self):
        self.fake("gh", "go", "codesign", "sudo")
        for args in (["--nope"], ["--build", "--build"], [""]):
            result = self.run_script("install.sh", *args)
            self.assertEqual(result.returncode, 2, args)
            self.assertIn("usage: sh install.sh [--build]", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_missing_version_fails(self):
        self.fake("gh", "sudo")
        self.manifest.write_text(json.dumps({"name": "ensemble"}, indent=2))
        result = self.run_script("install.sh", RELEASED=str(self.asset))
        self.assert_nothing_installed(result)
        self.assertIn(
            f"no version in {self.plugin.resolve()}/.claude-plugin/plugin.json", result.stderr
        )
        self.assertEqual(self.calls(), [])

    def test_missing_version_is_reported_before_missing_gh(self):
        self.fake("sudo")
        self.manifest.write_text(json.dumps({"name": "ensemble"}, indent=2))
        result = self.run_script("install.sh", RELEASED=str(self.asset))
        self.assert_nothing_installed(result)
        self.assertIn("no version in", result.stderr)
        self.assertNotIn("gh is required", result.stderr)

    def test_other_platforms_stop_before_download_naming_build(self):
        self.fake("gh", "sudo")
        for platform in ("Darwin x86_64", "Linux x86_64"):
            result = self.run_script("install.sh", RELEASED=str(self.asset), PLATFORM=platform)
            self.assert_nothing_installed(result)
            self.assertIn(f"not {platform}", result.stderr)
            self.assertIn(f"sh {self.plugin.resolve()}/broker/install.sh --build", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_build_seeds_definition_builds_signs_and_installs(self):
        self.fake("gh", "go", "codesign", "sudo")
        result = self.run_script("install.sh", "--build", PLATFORM="Darwin x86_64")
        self.assertEqual(result.returncode, 0, result.stderr)
        seeded = Path(self.ensemble, "docker/worker/Dockerfile")
        self.assertEqual(seeded.read_text(), (BROKER / "worker/Dockerfile").read_text())
        self.assertTrue(self.seeded.exists())
        calls = self.calls()
        self.assertEqual([c.split()[0] for c in calls], ["go"] + ["codesign"] * 4 + ["sudo"] * 2)
        out = calls[0].split()[4]
        self.assertEqual(calls[0], f"go build -trimpath -o {out} .")
        self.assertEqual(calls[1], f"{SIGN}{out}")
        self.assert_installed(calls[5:], out)

    def test_existing_definition_is_never_overwritten(self):
        self.fake("gh", "go", "codesign", "sudo")
        worker = Path(self.ensemble, "docker/worker")
        worker.mkdir(parents=True)
        (worker / "Dockerfile").write_text("FROM mine\n")
        for args in ([], ["--build"], []):
            result = self.run_script(
                "install.sh", *args, RELEASED=str(self.asset), ATTESTED_REF="refs/tags/v9.8.7"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((worker / "Dockerfile").read_text(), "FROM mine\n")
        self.assertEqual(os.listdir(worker), ["Dockerfile"])

    def test_build_without_go_fails_naming_go(self):
        self.fake("gh", "codesign", "sudo")
        result = self.run_script("install.sh", "--build")
        self.assert_nothing_installed(result)
        self.assertIn("go is required", result.stderr)
        self.assertEqual(self.calls(), [])
