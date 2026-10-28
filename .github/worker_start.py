import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECKS = ROOT / "src/broker/internal/launcher/checks"


def docker(*args, **kwargs):
    return subprocess.run(["docker", *args], check=True, capture_output=True, **kwargs).stdout


def main(image):
    labels = json.loads(docker("image", "inspect", image))[0]["Config"]["Labels"]
    uid, gid = labels["ensemble.agent-uid"], labels["ensemble.agent-gid"]
    for client, hook in (
        ("claude", ""),
        ("claude", "/usr/local/bin/ci-start-hook"),
        ("codex", "/usr/local/bin/ci-start-hook"),
    ):
        flags = ["--cap-add", "SETUID", "--cap-add", "SETGID"] if hook else []
        container = (
            docker(
                "run",
                "--detach",
                "--interactive",
                "--network",
                "none",
                "--user",
                "agent",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                *flags,
                "--tmpfs",
                f"/tmp/ci-state:uid={uid},gid={gid},mode=0700",
                "--entrypoint",
                "/usr/bin/tini",
                image,
                "--",
                "/opt/ensemble/venv/bin/python",
                "-I",
                "/opt/ensemble/src/ensemble/session/harness/guard.py",
                "--serve",
            )
            .decode()
            .strip()
        )
        try:
            docker(
                "exec",
                "--interactive",
                "--user",
                "root" if hook else "agent",
                container,
                "/usr/bin/python3",
                "-I",
                "-",
                client,
                "/tmp/ci-state",
                "/tmp",
                hook,
                (CHECKS / "image-content-check").read_text(),
                input=(CHECKS / "start-container.py").read_bytes(),
            )
            docker(
                "exec",
                "--interactive",
                "--user",
                "agent",
                container,
                "/usr/bin/python3",
                "-I",
                "-",
                client,
                hook,
                uid,
                gid,
                input=(ROOT / ".github/worker_start_probe.py").read_bytes(),
            )
        finally:
            docker("rm", "--force", container)
    print("Container bootstrap identities and capabilities verified")


if __name__ == "__main__":
    main(sys.argv[1])
