import subprocess
import sys

CONTENT_FAILED = 41
BOOTSTRAP_FAILED = 42
USAGE = 64


def main(argv):
    if len(argv) != 4:
        return USAGE
    client, directory, cwd, content = argv
    stage = CONTENT_FAILED
    try:
        if subprocess.run(["/bin/sh", "-s"], input=content.encode(), check=False).returncode:
            return stage
        stage = BOOTSTRAP_FAILED
        if (
            client != "opencode"
            and subprocess.run(
                [
                    "/opt/ensemble/venv/bin/python",
                    "-I",
                    "/opt/ensemble/lib/bootstrap.py",
                    client,
                    directory,
                    cwd,
                ],
                check=False,
            ).returncode
        ):
            return stage
    except OSError:
        return stage
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
