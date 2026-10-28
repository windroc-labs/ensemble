import base64
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECKS = ROOT / "src/broker/internal/launcher/checks"


def docker(*args, **kwargs):
    return subprocess.run(["docker", *args], check=True, capture_output=True, **kwargs).stdout


def main(image):
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_OS") != "Linux":
        sys.exit("container startup checks run only in Linux CI")
    if not re.fullmatch(r"ensemble-worker:[0-9a-f]{64}", image):
        sys.exit("container startup checks require a broker-built worker image")
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
            value = b"synthetic-start-credential\x00\xff"
            payload = json.dumps(
                [
                    {
                        "argv": ["/usr/bin/tee", "/tmp/ci-state/credential"],
                        "stdin": base64.b64encode(value).decode(),
                    }
                ]
            ).encode()
            result = docker(
                "exec",
                "--interactive",
                "--user",
                "agent",
                container,
                "/opt/ensemble/venv/bin/python",
                "-I",
                "/opt/ensemble/lib/credential-batch.py",
                input=payload,
            )
            assert json.loads(result) == {"completed": 1}
            assert (
                docker("exec", "--user", "agent", container, "cat", "/tmp/ci-state/credential")
                == value
            )
        finally:
            docker("rm", "--force", container)
    print("Container bootstrap identities and capabilities verified")


if __name__ == "__main__":
    main(sys.argv[1])
