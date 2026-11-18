"""scripts/install-target-egress.sh run with a fake broker, gh, docker and egress-install, in a temporary home and
root.
"""

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

from tests.case import REPO, Case

SCRIPT = os.path.join(REPO, "scripts", "install-target-egress.sh")
VERSION = "1.4.0"
DIGEST = "sha256:" + "ab" * 32


def pinned(name):
    """The script's pin assignment `name=<value>`, so a pin update is one edit, in the script."""
    (value,) = re.findall(rf"^{name}=(.*)$", Path(SCRIPT).read_text(), re.M)
    return value


PINNED_REF = pinned("egress_ref")

# The egress-install gh serves: records its argv, PATH and Docker variables, then exits $EGRESS_EXIT.
FAKE_INSTALL = """#!/bin/bash
{
  printf '%s\\n' "$@"
  printf 'PATH=%s\\n' "$PATH"
  printf 'DOCKER_HOST=%s\\n' "${DOCKER_HOST-unset}"
  printf 'DOCKER_CONTEXT=%s\\n' "${DOCKER_CONTEXT-unset}"
  printf 'DOCKER_CONFIG=%s\\n' "${DOCKER_CONFIG-unset}"
} >"$FAKES/install.log"
exit "${EGRESS_EXIT:-0}"
"""

# gh logs its argv; `gh api` prints the fake egress-install unless $FAKES/gh_fails or $FAKES/gh_empty exists.
FAKE_GH = """#!/bin/bash
printf '%s\\n' "$*" >>"$FAKES/gh.log"
[ -e "$FAKES/gh_fails" ] && exit 1
[ -e "$FAKES/gh_empty" ] && exit 0
cat "$FAKES/egress-install"
"""


# The broker answers `target` from $FAKES/target.out (exit 0), else $FAKES/target.err (exit 7); it logs its argv.
FAKE_BROKER = """#!/bin/bash
printf '%s\\n' "$*" >>"$FAKES/broker.log"
if [ -e "$FAKES/target.err" ]; then cat "$FAKES/target.err" >&2; exit 7; fi
cat "$FAKES/target.out"
"""
LOCAL = {
    "name": "local",
    "endpoint": "unix:///Users/u/.docker/run/docker.sock",
    "remote": False,
    "path": "/usr/bin:/bin:/usr/sbin:/sbin",
    "id": "L1",
}


def executable(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class InstallTargetEgressTest(Case):
    def setUp(self):
        self.root = Path(self.home, "root")
        self.fakes = Path(self.home, "fakes")
        self.bin = self.fakes / "bin"
        self.tmp = Path(self.home, "tmp")
        self.tmp.mkdir()
        self.wrapper = self.root / "usr/local/libexec/ensemble/ssh"
        self.path = f"{self.wrapper}:/usr/bin:/bin:/usr/sbin:/sbin"
        executable(self.root / "usr/local/libexec/ensemble/ensemble-broker", FAKE_BROKER)
        executable(self.bin / "gh", FAKE_GH)
        executable(self.bin / "docker", "#!/bin/sh\nexit 0\n")
        executable(self.fakes / "egress-install", FAKE_INSTALL)
        # Only the system tools the script and fakes run: no real gh or docker on PATH.
        self.system = self.fakes / "system"
        for tool in ("bash", "python3", "mktemp", "rm", "env", "cat"):
            executable(self.system / tool, f'#!/bin/sh\nexec {shutil.which(tool)} "$@"\n')
        self.target(None)

    def target(self, endpoint, *, unreachable=None):
        """The broker's target output: local, beside a remote daemon at `endpoint` (None: none)."""
        daemons = [LOCAL]
        if endpoint is not None:
            remote = {"name": "remote", "endpoint": endpoint, "remote": True, "path": self.path}
            remote.update({"unreachable": unreachable} if unreachable else {"id": "R1"})
            daemons.insert(0, remote)
        default = "local" if endpoint is None else "remote"
        found = {"default": default, "daemons": daemons}
        (self.fakes / "target.out").write_text(json.dumps(found) + "\n")

    def target_error(self, message):
        error = {"command": "target", "code": "docker_unreachable", "message": message}
        found = {"exit_code": 7, "error": {**error, "cleanup": "not_needed"}}
        (self.fakes / "target.err").write_text(json.dumps(found) + "\n")

    def run_script(self, *args, **env):
        base = {
            "HOME": self.home,
            "PATH": f"{self.bin}:{self.system}",
            "TMPDIR": str(self.tmp),
            "FAKES": str(self.fakes),
            "ENSEMBLE_INSTALL_ROOT": str(self.root),
            "DOCKER_HOST": "tcp://hostile:1234",
            "DOCKER_CONTEXT": "hostile",
            "DOCKER_CONFIG": "/hostile",
        }
        return subprocess.run(
            [shutil.which("bash"), SCRIPT, *args],
            env={**base, **env},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )

    def gh_calls(self):
        log = self.fakes / "gh.log"
        return log.read_text().splitlines() if log.exists() else []

    def installed(self):
        log = self.fakes / "install.log"
        return log.read_text().splitlines() if log.exists() else None

    def test_runs_egress_install_against_the_brokers_target(self):
        self.target("ssh://me@pc:2222")
        for code in (0, 11):
            with self.subTest(code=code):
                done = self.run_script(
                    "--version", VERSION, "--digest", DIGEST, EGRESS_EXIT=str(code)
                )
                self.assertEqual(done.returncode, code, done.stderr)
                self.assertEqual(
                    self.gh_calls()[-1],
                    "api -H Accept: application/vnd.github.raw "
                    f"repos/windroc-labs/egress-proxy/contents/egress-install?ref={PINNED_REF}",
                )
                lines = self.installed()
                self.assertEqual(
                    lines[:8],
                    [
                        "--version",
                        VERSION,
                        "--digest",
                        DIGEST,
                        "--dir",
                        f"{self.home}/.egress-target",
                        "--docker-host",
                        "ssh://me@pc:2222",
                    ],
                )
                self.assertEqual(lines[8], f"PATH={self.path}:{self.bin}:{self.system}")
                self.assertEqual(
                    lines[9:], ["DOCKER_HOST=unset", "DOCKER_CONTEXT=unset", "DOCKER_CONFIG=unset"]
                )
                if code:
                    self.assertIn("egress-install exited 11", done.stderr)
                else:
                    self.assertIn("ssh://me@pc:2222", done.stderr)
                    self.assertIn(
                        f"PATH={self.path}:$PATH {self.home}/.egress-target/start.sh",
                        done.stderr,
                    )
                self.assertEqual(list(self.tmp.iterdir()), [])

    def test_no_remote_daemon_exits_2_without_fetching(self):
        done = self.run_script("--version", VERSION, "--digest", DIGEST)
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn("no remote Docker target is set", done.stderr)
        self.assertIn("scripts/install.sh --docker", done.stderr)
        self.assertEqual(self.gh_calls(), [])

    def test_an_invalid_target_setting_exits_2_with_the_brokers_reason(self):
        message = "Cannot reach Docker target /Users/u/.ensemble/docker/target.json: invalid target setting (no host)."
        self.target_error(message)
        done = self.run_script("--version", VERSION, "--digest", DIGEST)
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn(message, done.stderr)
        self.assertIn("invalid Docker target setting; fix it, then rerun", done.stderr)
        self.assertEqual(self.gh_calls(), [])

    def test_an_unreachable_remote_exits_1_before_egress_install(self):
        reason = f"ssh wrapper unavailable: {self.wrapper}/ssh; reinstall the broker"
        self.target("ssh://pc", unreachable=reason)
        done = self.run_script("--version", VERSION, "--digest", DIGEST)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn(f"cannot reach the remote Docker target ssh://pc: {reason}", done.stderr)
        self.assertEqual(self.gh_calls(), [])
        self.assertIsNone(self.installed())

    def test_a_failing_broker_exits_1(self):
        (self.root / "usr/local/libexec/ensemble/ensemble-broker").unlink()
        done = self.run_script("--version", VERSION, "--digest", DIGEST)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("install or upgrade the broker with scripts/install.sh --docker", done.stderr)
        self.assertEqual(self.gh_calls(), [])

    def test_the_target_comes_from_the_broker_only(self):
        self.target("ssh://u@pc:2222")
        target_file = Path(self.home, ".ensemble/docker/target.json")
        target_file.parent.mkdir(parents=True)
        target_file.write_text('{"host": "ssh://other@elsewhere", "local_secrets": []}')
        done = self.run_script("--version", VERSION, "--digest", DIGEST)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.installed()[6:8], ["--docker-host", "ssh://u@pc:2222"])
        self.assertTrue(self.installed()[8].startswith(f"PATH={self.path}:"))
        self.assertEqual((self.fakes / "broker.log").read_text().splitlines(), ["target"])

    def test_without_flags_installs_the_pin_or_exits_2_while_unpinned(self):
        self.target("ssh://pc")
        version, digest = pinned("egress_version"), pinned("egress_digest")
        done = self.run_script()
        if version:
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(self.installed()[:4], ["--version", version, "--digest", digest])
        else:
            self.assertEqual(done.returncode, 2, done.stderr)
            self.assertIn("no egress-proxy release with remote install is pinned yet", done.stderr)
            self.assertIn("--version", done.stderr)
            self.assertEqual(self.gh_calls(), [])

    def test_accepted_target_hosts(self):
        for host in ("ssh://pc", "ssh://u@h:65535", "ssh://me@pc.tailnet.ts.net"):
            with self.subTest(host=host):
                self.target(host)
                done = self.run_script("--version", VERSION, "--digest", DIGEST)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(self.installed()[7], host)

    def test_missing_tools_exit_1_without_fetching(self):
        self.target("ssh://pc")
        for tool in ("gh", "docker"):
            with self.subTest(tool=tool):
                hidden = self.bin / tool
                hidden.rename(self.fakes / f"{tool}.hidden")
                done = self.run_script("--version", VERSION, "--digest", DIGEST)
                (self.fakes / f"{tool}.hidden").rename(hidden)
                self.assertEqual(done.returncode, 1, done.stderr)
                self.assertIn(f"needs {tool} on PATH", done.stderr)
                self.assertEqual(self.gh_calls(), [])

    def test_missing_python3_is_not_blamed_on_the_target(self):
        self.target("ssh://pc")
        (self.system / "python3").unlink()
        done = self.run_script("--version", VERSION, "--digest", DIGEST)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("needs a working python3", done.stderr)
        self.assertNotIn("target setting;", done.stderr)
        self.assertEqual(self.gh_calls(), [])

    def test_version_and_digest_validation(self):
        self.target("ssh://pc")
        for args in (
            ("--version", VERSION),
            ("--digest", DIGEST),
            ("--version", "1.4", "--digest", DIGEST),
            ("--version", VERSION, "--digest", "sha256:" + "AB" * 32),
            ("--version", VERSION, "--digest", "sha256:abc"),
            ("--version",),
            ("--bogus",),
        ):
            with self.subTest(args=args):
                done = self.run_script(*args)
                self.assertEqual(done.returncode, 2, done.stderr)
                self.assertIn("usage: install-target-egress.sh", done.stderr)
                self.assertEqual(self.gh_calls(), [])

    def test_help_prints_usage(self):
        done = self.run_script("--help")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("usage: install-target-egress.sh", done.stdout)

    def test_fetch_failure_exits_1_and_cleans_up(self):
        self.target("ssh://pc")
        for marker in ("gh_fails", "gh_empty"):
            with self.subTest(marker=marker):
                (self.fakes / marker).write_text("")
                done = self.run_script("--version", VERSION, "--digest", DIGEST)
                (self.fakes / marker).unlink()
                self.assertEqual(done.returncode, 1, done.stderr)
                self.assertIn(PINNED_REF, done.stderr)
                self.assertIsNone(self.installed())
                self.assertEqual(list(self.tmp.iterdir()), [])
