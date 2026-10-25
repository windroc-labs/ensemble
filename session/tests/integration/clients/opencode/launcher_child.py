"""A child for launcher_integration_test.py, by its first argument: echo (stdin back as JSON, then
exit 3), silent (no session), spawn (a sleeping descendant holding the pipes), bound (a session,
then more output after a pause), sleep SECONDS.
"""

import json
import subprocess
import sys
import time

BOUND = json.dumps({"type": "step_start", "sessionID": "ses_a"})


def main(mode: str) -> int:
    if mode == "echo":
        text = sys.stdin.read()
        print(BOUND)
        print(json.dumps({"text": text}))
        print("oops", file=sys.stderr)
        return 3
    if mode == "silent":
        print(json.dumps({"type": "text"}), flush=True)
        time.sleep(60)
    elif mode == "spawn":
        subprocess.Popen([sys.executable, __file__, "sleep", "10"])
        time.sleep(60)
    elif mode == "bound":
        print(BOUND, flush=True)
        time.sleep(2)
        print("done")
    elif mode == "sleep":
        time.sleep(float(sys.argv[2]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
