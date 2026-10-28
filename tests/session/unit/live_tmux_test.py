import os
from unittest import mock

from tests.session import live_tmux
from tests.session.case import Case


class EventsTest(Case):
    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.home, "events")
        self.expected = ["worker-drive outcome done", "worker-drive exited 0"]
        self.notifications = [("worker-drive", word) for word in ("outcome", "exited")]

    def write(self, events):
        with open(self.path, "w") as stream:
            stream.writelines(f"12:00:00 {event}\n" for event in events)

    def test_both_orders_are_complete_and_duplicates_remain_visible(self):
        for events in (self.expected, self.expected[::-1], self.expected * 2):
            with self.subTest(events=events):
                self.write(events)
                lines = live_tmux.wait_events(self.path, self.notifications, timeout=0)
                self.assertEqual(lines, [f"12:00:00 {event}" for event in events])

    def test_exited_alone_waits_for_the_independent_outcome_writer(self):
        self.write(self.expected[1:])
        with mock.patch("time.sleep", side_effect=lambda _: self.write(self.expected[::-1])):
            lines = live_tmux.wait_events(self.path, self.notifications)
        self.assertEqual(lines, [f"12:00:00 {event}" for event in self.expected[::-1]])

    def test_missing_events_and_an_unfinished_line_do_not_complete(self):
        for events in ([], self.expected[:1], self.expected[1:]):
            with self.subTest(events=events):
                self.write(events)
                with self.assertRaises(AssertionError):
                    live_tmux.wait_events(self.path, self.notifications, timeout=0)
        with open(self.path, "w") as stream:
            stream.write("12:00:00 worker-drive exited 0\n12:00:00 worker-drive outcome done")
        with self.assertRaises(AssertionError):
            live_tmux.wait_events(self.path, self.notifications, timeout=0)
