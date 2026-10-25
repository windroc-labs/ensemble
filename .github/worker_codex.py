"""CI-only synthetic checks of the worker's Codex authentication, plugins and mount layout."""

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[1] / "broker/internal/launcher/assets/Dockerfile"
PYTHON = "/opt/ensemble/venv/bin/python"
BOOTSTRAP = "/opt/ensemble/lib/bootstrap.py"
PROBES = Path(__file__).with_suffix("")
INSTALLED = b"/opt/ensemble/clients/lib/node_modules/@openai/codex"


def synthetic_credentials(lifetime=540):
    now = int(time.time())
    refreshed = datetime.fromtimestamp(now, UTC).isoformat().replace("+00:00", "Z")
    documents = []
    for marker in ("initial", "replacement"):
        claims = {
            "exp": now + lifetime,
            "sub": "ci-fake-user",
            "email": "ci-probe@example.invalid",
            "jti": marker + "-" + uuid.uuid4().hex,
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "ci-fake",
                "chatgpt_user_id": "ci-fake-user",
                "chatgpt_plan_type": "plus",
            },
        }
        encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
        access = f"e30.{encoded}.synthetic"
        documents.append(
            json.dumps(
                {
                    "auth_mode": "chatgpt",
                    "OPENAI_API_KEY": None,
                    "tokens": {
                        "id_token": access,
                        "access_token": access,
                        "refresh_token": "",
                        "account_id": "ci-fake",
                    },
                    "last_refresh": refreshed,
                }
            ).encode()
        )
    return tuple(documents)


def docker(*args, data=None):
    result = subprocess.run(["docker", *args], input=data, capture_output=True, timeout=120)
    if result.returncode:
        print(result.stderr.decode(errors="replace")[-8000:], file=sys.stderr)
        raise RuntimeError(f"synthetic Codex check: docker {args[0]} failed")
    return result.stdout


def probe(name, program, *args, user="agent"):
    return docker(
        "exec",
        "-i",
        "--user",
        user,
        name,
        PYTHON,
        "-I",
        "-",
        *args,
        data=(PROBES / program).read_bytes(),
    )


def single_installation(image):
    found = docker(
        "run",
        "--rm",
        "--network",
        "none",
        "--user",
        "root",
        image,
        "find",
        "/",
        "-xdev",
        "-type",
        "d",
        "-path",
        "*/node_modules/@openai/codex",
        "-prune",
    ).split()
    if found != [INSTALLED]:
        raise RuntimeError(f"synthetic Codex check: @openai/codex packages {found}")


def latest_runtime(directory, architecture):
    metadata = json.loads(
        subprocess.check_output(["npm", "view", "@openai/codex@latest", "--json"], timeout=60)
    )
    version = metadata["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("latest Codex must be stable")
    platform = "@openai/codex-linux-" + {"amd64": "x64", "arm64": "arm64"}[architecture]
    dependency = metadata["optionalDependencies"][platform]
    package = (
        dependency.removeprefix("npm:")
        if dependency.startswith("npm:")
        else platform + "@" + dependency
    )
    [packed] = json.loads(
        subprocess.check_output(
            ["npm", "pack", "--json", "--ignore-scripts", "--pack-destination", directory, package],
            cwd=directory,
            timeout=90,
        )
    )
    return version, Path(directory, packed["filename"]).read_bytes()


def main(image):
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("CI") != "true":
        sys.exit("worker_codex.py runs only in GitHub CI")
    if not re.fullmatch(r"ensemble-worker:[0-9a-f]{64}", image):
        sys.exit("expected a broker-built worker image tag")
    [pinned] = re.findall(r"@openai/codex@(\S+)", DOCKERFILE.read_text(encoding="utf-8"))
    version = f"codex-cli {pinned}".encode()
    inspected = json.loads(docker("image", "inspect", image))[0]
    print(f"Worker image size: {inspected['Size']} bytes")
    single_installation(image)
    labels = inspected["Config"]["Labels"]
    owner = f",mode=0700,uid={labels['ensemble.agent-uid']},gid={labels['ensemble.agent-gid']}"
    with tempfile.TemporaryDirectory(
        dir=os.environ["RUNNER_TEMP"], prefix="codex-state-"
    ) as temporary:
        home = Path(temporary, "codex")
        sessions = home / "sessions"
        sessions.mkdir(parents=True)
        sessions.chmod(0o777)
        architecture = inspected["Architecture"]
        latest, bundle = latest_runtime(temporary, architecture)
        archive = Path(temporary, "runtime.tgz")
        archive.write_bytes(bundle)
        for _ in range(2):
            name = "ensemble-codex-ci-" + uuid.uuid4().hex
            try:
                docker(
                    "run",
                    "--detach",
                    "--name",
                    name,
                    "--network",
                    "none",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--user",
                    "agent",
                    "--env",
                    f"CODEX_HOME={home}",
                    "--tmpfs",
                    f"{home}:rw,noexec,nosuid,nodev,size=1073741824{owner}",
                    "--tmpfs",
                    f"/run/ensemble:rw,noexec,nosuid,nodev,size=1048576{owner}",
                    "--tmpfs",
                    f"/run/ensemble/secrets:rw,noexec,nosuid,nodev,size=65536{owner}",
                    "--tmpfs",
                    "/opt/ensemble/runtime:rw,exec,nosuid,nodev,size=536870912,mode=0755,uid=0,gid=0",
                    "--mount",
                    f"type=bind,source={sessions},target={sessions}",
                    image,
                )
                inspected = json.loads(docker("inspect", name))[0]
                mounts = inspected["Mounts"]
                assert [(m["Source"], m["Destination"]) for m in mounts if m["Type"] == "bind"] == [
                    (str(sessions), str(sessions))
                ]
                assert {str(home), "/run/ensemble", "/run/ensemble/secrets"} <= {
                    *inspected["HostConfig"]["Tmpfs"]
                }
                docker("exec", name, PYTHON, "-I", BOOTSTRAP, "codex", str(home), "/workspace")
                docker("cp", str(archive), name + ":/tmp/codex-runtime.tgz")
                probe(name, "stage.py", "/tmp/codex-runtime.tgz", user="root")
                assert (
                    docker(
                        "exec", name, "/opt/ensemble/runtime/codex.next/bin/codex", "--version"
                    ).strip()
                    == f"codex-cli {latest}".encode()
                )
                for action in ("publish", "reset"):
                    docker(
                        "exec",
                        "--user",
                        "root",
                        name,
                        PYTHON,
                        "-I",
                        "/opt/ensemble/lib/codex-update.py",
                        action,
                    )
                    actual = latest if action == "publish" else pinned
                    assert (
                        docker("exec", name, "codex", "--version").strip()
                        == f"codex-cli {actual}".encode()
                    )
                    docker(
                        "exec",
                        name,
                        PYTHON,
                        "-I",
                        "/opt/ensemble/lib/codex-update.py",
                        "probe",
                        "/opt/ensemble/bin/codex",
                        actual,
                    )
                    assert (
                        probe(name, "tools.py").strip()
                        == b"Offline Codex code-mode command turn: PASS"
                    )
                probe(name, "skills.py")
                document, replacement = synthetic_credentials()
                near, _ = synthetic_credentials(lifetime=120)
                _, adopted = synthetic_credentials()
                access = json.loads(document)["tokens"]["access_token"]
                docker(
                    "exec",
                    "-i",
                    name,
                    PYTHON,
                    "-I",
                    BOOTSTRAP,
                    "codex-auth",
                    str(home),
                    data=document,
                )
                assert docker("exec", name, "codex", "--version").strip() == version
                status = docker("exec", name, "codex", "login", "status")
                assert access.encode() not in status
                probe(name, "state.py")
                payload = json.dumps(
                    {
                        "initial": json.loads(document),
                        "replacement": json.loads(replacement),
                        "near": json.loads(near),
                        "adopted": json.loads(adopted),
                    }
                ).encode()
                checked = probe(name, "reload.py", payload.decode())
                assert checked.strip() == b"Synthetic same-process guarded reload: PASS"
            finally:
                docker("rm", "--force", name)
            assert sorted(p.name for p in home.iterdir()) == ["sessions"]
    print(
        "Codex updates, native skills, synthetic guarded reload, tmpfs and history persistence: PASS"
    )


if __name__ == "__main__":
    main(sys.argv[1])
