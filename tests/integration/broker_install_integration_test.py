import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

from tests.case import REPO, Case

BROKER = Path(REPO, "src", "broker")
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
    "shasum",
)
ASSET = "ensemble-broker-darwin-arm64.tar.gz"
TARGET = "/usr/local/libexec/ensemble"
SUMS = f"{ASSET}.sha256"
DOWNLOAD = "gh release download v9.8.7 --repo windroc-labs/ensemble --pattern"
# The released binary's stand-in: answers version with $ASSET_PROTOCOL and marks that it ran.
RELEASED_BINARY = b"""#!/bin/sh
: > "$RAN"
[ "$*" = version ] && printf '{"protocol_version":"%s"}\\n' "$ASSET_PROTOCOL"
"""
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
    case "$7" in *.sha256) from=${RELEASED_SUMS:-} ;; *) from=${RELEASED:-} ;; esac
    [ -n "$from" ] || { echo "release asset not found" >&2; exit 1; }
    cp "$from" "$9/$7" ;;
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
        (self.plugin / "src/broker/worker").mkdir(parents=True)
        for name in ("install.sh", "build.sh", "with-go-state.sh", "worker/Dockerfile"):
            shutil.copy(BROKER / name, self.plugin / "src/broker" / name)
        (self.plugin / ".claude-plugin").mkdir()
        self.manifest = self.plugin / ".claude-plugin/plugin.json"
        self.manifest.write_text(json.dumps({"name": "ensemble", "version": "9.8.7"}, indent=2))
        self.client = self.plugin / "src/ensemble/session/hosts/docker/broker.py"
        self.client.parent.mkdir(parents=True)
        self.client.write_text('BROKER = "/x"\nPROTOCOL = "7"\nTIMEOUT = 1\n')
        self.asset = Path(self.home, "asset.tar.gz")
        with tarfile.open(self.asset, "w:gz") as archive:
            member = tarfile.TarInfo("ensemble-broker")
            member.size = len(RELEASED_BINARY)
            member.mode = 0o755
            archive.addfile(member, io.BytesIO(RELEASED_BINARY))
        self.sums = Path(self.home, "asset.tar.gz.sha256")
        self.sums.write_text(f"{hashlib.sha256(self.asset.read_bytes()).hexdigest()}  {ASSET}\n")
        self.ran = Path(self.home, "asset-ran")
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
            "RAN": str(self.ran),
            "ASSET_PROTOCOL": "7",
        }

    def fake(self, *names):
        for name in names:
            path = self.bin / name
            path.write_text(FAKE)
            path.chmod(0o755)

    def run_script(self, name, *args, **env):
        return subprocess.run(
            [str(self.bin / "sh"), str(self.plugin / "src/broker" / name), *args],
            env={**self.env, **env},
            capture_output=True,
            text=True,
            timeout=60,
        )

    def install_release(self, *args, **env):
        return self.run_script(
            "install.sh", *args, RELEASED=str(self.asset), RELEASED_SUMS=str(self.sums), **env
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

    def test_downloads_checks_and_installs_the_release_asset(self):
        self.fake("gh", "sudo")
        result = self.install_release()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        work = calls[0].rsplit(" ", 1)[1]
        self.assertEqual(calls[0], f"{DOWNLOAD} {ASSET} --dir {work}")
        self.assertEqual(calls[1], f"{DOWNLOAD} {SUMS} --dir {work}")
        self.assert_installed(calls[2:], f"{work}/ensemble-broker")
        self.assertEqual(self.installed.read_bytes(), RELEASED_BINARY)
        seeded = Path(self.ensemble, "docker/worker/Dockerfile")
        self.assertEqual(seeded.read_text(), (BROKER / "worker/Dockerfile").read_text())
        self.assertTrue(self.seeded.exists())
        self.assertFalse(os.path.exists(work))

    def test_missing_asset_fails_naming_retry_and_build(self):
        self.fake("gh", "sudo")
        result = self.run_script("install.sh", RELEASED_SUMS=str(self.sums))
        self.assert_nothing_installed(result)
        self.assertEqual(len(self.calls()), 1)
        self.assertIn(f"could not download {ASSET} from release v9.8.7", result.stderr)
        self.assertIn("retry in a few minutes", result.stderr)
        self.assertIn(f"sh {self.plugin.resolve()}/src/broker/install.sh --build", result.stderr)

    def test_missing_sha256_file_aborts_before_install(self):
        self.fake("gh", "sudo")
        result = self.run_script("install.sh", RELEASED=str(self.asset))
        self.assert_nothing_installed(result)
        self.assertEqual(len(self.calls()), 2)
        self.assertIn(f"could not download {SUMS} from release v9.8.7", result.stderr)
        self.assertIn("nothing installed", result.stderr)
        self.assertIn(f"sh {self.plugin.resolve()}/src/broker/install.sh --build", result.stderr)
        self.assertFalse(self.ran.exists())

    def test_sha256_mismatch_aborts_before_install(self):
        self.fake("gh", "sudo")
        for sums in (f"{'0' * 64}  {ASSET}\n", "", "not a sha256\n"):
            self.sums.write_text(sums)
            result = self.install_release()
            self.assert_nothing_installed(result)
            self.assertIn(
                f"{ASSET} does not match the sha256 in {SUMS} of release v9.8.7; nothing installed",
                result.stderr,
            )
            self.assertFalse(self.ran.exists())

    def test_sha256_file_names_only_the_digest_first(self):
        self.fake("gh", "sudo")
        self.sums.write_text(hashlib.sha256(self.asset.read_bytes()).hexdigest() + "\n")
        result = self.install_release()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.installed.read_bytes(), RELEASED_BINARY)

    def test_no_call_verifies_an_attestation(self):
        self.fake("gh", "sudo")
        result = self.install_release()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse([c for c in self.calls() if "attestation" in c])
        self.assertNotIn("attestation", (BROKER / "install.sh").read_text())

    def test_local_asset_is_checked_against_its_sha256_file_then_installed(self):
        self.fake("gh", "sudo")
        result = self.run_script("install.sh", ENSEMBLE_BROKER_ASSET=str(self.asset))
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertRegex(calls[0], r"^sudo install -d ")
        work = calls[1].split()[8].rsplit("/", 1)[0]
        self.assertNotEqual(work, self.home)
        self.assert_installed(calls, f"{work}/ensemble-broker")
        self.assertEqual(self.installed.read_bytes(), RELEASED_BINARY)

    def test_local_asset_without_its_sha256_file_aborts(self):
        self.fake("gh", "sudo")
        self.sums.unlink()
        result = self.run_script("install.sh", ENSEMBLE_BROKER_ASSET=str(self.asset))
        self.assert_nothing_installed(result)
        self.assertIn(f"no sha256 file {self.sums}; nothing installed", result.stderr)
        self.assertFalse(self.ran.exists())

    def test_local_asset_with_another_sha256_aborts(self):
        self.fake("gh", "sudo")
        self.sums.write_text(f"{'f' * 64}  {ASSET}\n")
        result = self.run_script("install.sh", ENSEMBLE_BROKER_ASSET=str(self.asset))
        self.assert_nothing_installed(result)
        self.assertIn(f"{self.asset} does not match the sha256 in {self.sums}", result.stderr)
        self.assertFalse(self.ran.exists())

    def test_asset_with_another_protocol_aborts_before_install(self):
        self.fake("gh", "sudo")
        result = self.install_release(ASSET_PROTOCOL="6")
        self.assert_nothing_installed(result)
        self.assertTrue(self.ran.exists())
        self.assertIn(
            "v9.8.7's ensemble-broker speaks protocol 6, this plugin wants 7: install the plugin"
            " at v9.8.7 (docs/upgrading.md), or build the broker from this plugin with"
            f" sh {self.plugin.resolve()}/src/broker/install.sh --build, which needs Go and"
            " resets every broker login; nothing installed",
            result.stderr,
        )

    def test_unreadable_protocols_abort_before_install(self):
        self.fake("gh", "sudo")
        for asset, client, named in (
            ("x7", 'PROTOCOL = "7"\n', "speaks protocol unknown, this plugin wants 7"),
            ("7", "PROTOCOL = 7\n", "speaks protocol 7, this plugin wants unknown"),
        ):
            self.client.write_text(client)
            result = self.install_release(ASSET_PROTOCOL=asset)
            self.assert_nothing_installed(result)
            self.assertIn(named, result.stderr)

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
            self.assertIn(
                f"sh {self.plugin.resolve()}/src/broker/install.sh --build", result.stderr
            )
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
            result = self.install_release(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((worker / "Dockerfile").read_text(), "FROM mine\n")
        self.assertEqual(os.listdir(worker), ["Dockerfile"])

    def test_build_without_go_fails_naming_go(self):
        self.fake("gh", "codesign", "sudo")
        result = self.run_script("install.sh", "--build")
        self.assert_nothing_installed(result)
        self.assertIn("go is required", result.stderr)
        self.assertEqual(self.calls(), [])
