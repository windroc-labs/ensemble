import os
import pwd
import subprocess
import sys

PYTHON = "/opt/ensemble/venv/bin/python"
BOOTSTRAP = "/opt/ensemble/lib/bootstrap.py"


def main(argv):
    if len(argv) != 5:
        return 41
    client, directory, cwd, hook, content = argv
    identity = {}
    if hook:
        agent = pwd.getpwnam("agent")
        identity = {
            "user": agent.pw_uid,
            "group": agent.pw_gid,
            "extra_groups": [],
            "env": {**os.environ, "HOME": agent.pw_dir},
        }
    stage = 41
    try:
        if subprocess.run(
            ["/bin/sh", "-s"], input=content.encode(), check=False, **identity
        ).returncode:
            return stage
        stage = 42
        if (
            client != "opencode"
            and subprocess.run(
                [PYTHON, "-I", BOOTSTRAP, client, directory, cwd], check=False, **identity
            ).returncode
        ):
            return stage
        stage = 43
        if hook and subprocess.run([PYTHON, "-I", BOOTSTRAP, "hook", hook], check=False).returncode:
            return stage
    except OSError:
        return stage
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
