"""An event handler for the session harness integration tests: each EventCall appended as one JSON
line to the file its state's `path` names; with a `pause` file in its state, Exited waits (at most
CAP s) for that file, holding its harness in its drain.
"""

import json
import os
import time

CAP = 60


def on_event(call) -> None:
    record = {
        "type": type(call.event).__name__,
        "event_id": call.event_id,
        "turn": call.turn,
        "sid": call.sid,
        "generation": call.generation,
        "reply": getattr(call.event, "reply", None),
    }
    with open(call.state["path"], "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    pause = call.state.get("pause")
    if pause and record["type"] == "Exited":
        until = time.monotonic() + CAP
        while not os.path.exists(pause) and time.monotonic() < until:
            time.sleep(0.05)
