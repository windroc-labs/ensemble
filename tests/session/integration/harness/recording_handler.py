"""An event handler for the session harness integration tests: each EventCall appended as one JSON
line to the file its state's `path` names.
"""

import json


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
