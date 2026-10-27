"""An event handler for the session harness integration tests: each EventCall appended as one JSON
line to the file its state's `path` names; with a `pause` file in its state, Exited waits (at most
CAP s) for that file, holding its harness in its drain; with a `send` file in its state, a TurnEnd
sends that file's text through its view first, then removes the file (its record's `sent`).
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
        "error": getattr(call.event, "error", None),
        "detail": getattr(call.event, "detail", None),
        "returncode": getattr(call.event, "returncode", None),
    }
    send = call.state.get("send")
    if send and record["type"] == "TurnEnd" and os.path.exists(send):
        with open(send, encoding="utf-8") as f:
            record["sent"] = f.read()
        call.session.send(record["sent"])  # raising: Dispatch calls again with this EventCall
        os.remove(send)
    with open(call.state["path"], "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    pause = call.state.get("pause")
    if pause and record["type"] == "Exited":
        until = time.monotonic() + CAP
        while not os.path.exists(pause) and time.monotonic() < until:
            time.sleep(0.05)
