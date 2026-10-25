"""FakeHost's standalone supervisor: program output and terminal input stay in the session dir;
ProcessControl owns the process group, while exited/dead events and rc track the program. It exits
when serve() retires its controller.
"""

import contextlib
import json
import os
import shutil
import subprocess
import sys

from ensemble.session.engine.status_log import SessionStatusLog
from ensemble.utils import ProcessControl, save


def main(folder: str, events: str) -> None:
    with open(os.path.join(folder, "meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    with open(os.path.join(folder, "env.json"), encoding="utf-8") as f:
        env = json.load(f)
    stdin = subprocess.DEVNULL
    if meta["kind"] == "terminal":
        os.mkfifo(os.path.join(folder, "input"), 0o600)
        stdin = os.open(os.path.join(folder, "input"), os.O_RDWR)

    def exited(rc):
        if events:
            name = os.path.basename(folder)
            word = ("exited", rc) if meta["kind"] == "detach" else ("dead",)
            with contextlib.suppress(OSError):
                SessionStatusLog(events).write(name, *word)
        save(os.path.join(folder, "rc"), str(rc))
        if meta["kind"] == "detach":
            shutil.rmtree(folder)

    save(os.path.join(folder, "pid"), str(os.getpid()))
    with open(os.path.join(folder, "output"), "ab") as output:
        ProcessControl(meta["control"]).serve(
            env=env, stdin=stdin, stdout=output, stderr=output, on_exit=exited
        )


if __name__ == "__main__":
    main(*sys.argv[1:])
