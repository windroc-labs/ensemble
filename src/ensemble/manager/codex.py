"""Configure the Codex connection from the manager's live hold. Standalone, stdlib only."""

import json
import os
import subprocess
import sys


def main(root: str, argv: tuple[str, ...]) -> int:
    excluded = [
        os.path.join(root, "skills", "manage", "scripts", "codex"),
        os.path.expanduser("~/.ensemble/bin/codex"),
    ]
    if argv[:1] == ("--ensemble-launcher",):
        excluded.append(argv[1])
        argv = argv[2:]
    config: dict = {"excluded": excluded}
    if os.environ.get("ENSEMBLE_MANAGER") and "ENSEMBLE_AGENT_SESSION" not in os.environ:
        recorder = (
            sys.executable,
            "-I",
            os.path.join(root, "src", "ensemble", "manager", "rearm.py"),
        )
        context = subprocess.run(
            (*recorder, "--binding", "context"),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if context.returncode == 0 and context.stdout:
            identity = json.loads(context.stdout)
            config.update(
                ancestor=identity["holder_pid"],
                recorder=(*recorder, "--binding", "record", "--client", "codex", "--client-pid"),
                launch_recorder=(
                    *recorder,
                    "--binding",
                    "launch",
                    "--client",
                    "codex",
                    "--client-pid",
                ),
                environ={
                    key: value
                    for key, value in os.environ.items()
                    if key.startswith("ENSEMBLE_HOLDER_") or key == "ENSEMBLE_MANAGER"
                },
                local=any(
                    name in os.environ
                    for name in ("CODEX_EXEC_SERVER_URL", "OPENAI_WORKLOAD_IDENTITY_CONTEXT")
                ),
            )
    bridge = os.path.join(root, "src/ensemble/session/clients/codex/manager.py")
    os.execv(sys.executable, (sys.executable, "-I", bridge, json.dumps(config), *argv))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1], tuple(sys.argv[2:])))
    except (
        OSError,
        ValueError,
        KeyError,
        IndexError,
        TypeError,
        subprocess.SubprocessError,
    ) as error:
        sys.exit(f"codex: {error}")
