"""Event and ledger fixtures shared by logic and process-death tests."""

import json
import os
import time
from pathlib import Path
from unittest import mock

from ensemble.manager.engine.events import EVENTS_MAX, SETTLE, Events
from ensemble.utils import append_line, file_lock, save
from tests.manager.case import Case


def settle_at_once():
    """time.sleep patched to return at once for SETTLE and really sleep for any other time."""
    sleep = time.sleep
    return mock.patch("time.sleep", side_effect=lambda s: None if s == SETTLE else sleep(s))


class EventsCase(Case):
    def setUp(self):
        self.enterContext(settle_at_once())
        self.directory = os.path.join(self.home, "events-test")
        os.mkdir(self.directory, 0o700)
        self.events = Events(self.directory)
        self.original = b"h" * EVENTS_MAX + b"\nold\n"
        append_line(self.events.path, self.original, locked=True)
        self.ledger = Path(self.directory, "ledger.json")
        save(self.ledger, '{"gen":0,"cursor":1}\n', durable=True)

    def generation(self):
        return json.loads(self.ledger.read_text())["gen"]

    def recover(self):
        with file_lock(self.events.path):
            self.events.recover(self.generation())
