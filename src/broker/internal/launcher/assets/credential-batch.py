import base64
import json
import subprocess
import sys


def deliver(stream):
    completed = 0
    try:
        commands = json.load(stream)
        if not isinstance(commands, list):
            return {"completed": 0}
        prepared = []
        for command in commands:
            if (
                not isinstance(command, dict)
                or set(command) != {"argv", "stdin"}
                or not isinstance(command["argv"], list)
                or not command["argv"]
                or any(not isinstance(arg, str) or "\0" in arg for arg in command["argv"])
                or command["stdin"] is not None
                and not isinstance(command["stdin"], str)
            ):
                return {"completed": 0}
            data = base64.b64decode(command["stdin"] or "", validate=True)
            prepared.append((command["argv"], data))
        for argv, data in prepared:
            result = subprocess.run(
                argv,
                input=data,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if result.returncode:
                break
            completed += 1
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        pass
    return {"completed": completed}


if __name__ == "__main__":
    json.dump(deliver(sys.stdin), sys.stdout)
