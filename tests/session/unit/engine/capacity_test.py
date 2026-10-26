"""Client admission across configurations; filesystem locks, no processes or credentials."""

import json
import os
import threading
from pathlib import Path
from unittest import mock

from ensemble.session.engine.capacity import Admission
from ensemble.session.engine.types import Liveness, Mode, SessionError, SessionRow
from tests.session.case import Case
from tests.session.fakes.terminal import MemoryHost


class AdmissionTest(Case):
    def setUp(self):
        self.host = MemoryHost()

    def enter(
        self, *, client="fake", limit=1, name="first", sid="s1", mode=Mode.HEADLESS, hosts=None
    ):
        ticket = Admission(
            client,
            limit,
            name=name,
            sid=sid,
            mode=mode,
            hosts={"h": self.host} if hosts is None else hosts,
        )
        self.addCleanup(ticket.close)
        return ticket

    def test_live_transient_reservation_counts_and_failure_rolls_back(self):
        live = mock.Mock(return_value=Liveness.LIVE)
        first = self.enter()
        with first:
            first.started(live)
        with (
            self.assertRaisesRegex(SessionError, "client fake.*limit 1.*first"),
            self.enter(name="second", sid="s2"),
        ):
            self.fail("admitted")
        first.close()
        with self.assertRaisesRegex(ValueError, "failed"), self.enter():
            raise ValueError("failed")
        with self.enter():
            pass

    def test_unlimited_runs_are_counted_by_a_limited_caller_and_clients_are_separate(self):
        first = self.enter(limit=0, name=None)
        with first:
            first.started(lambda: Liveness.LIVE)
        with self.assertRaisesRegex(SessionError, "s1"), self.enter():
            self.fail("admitted")
        with self.enter(client="other"):
            pass

    def test_transient_completion_releases_without_consuming_events(self):
        live = mock.Mock(return_value=Liveness.LIVE)
        first = self.enter()
        with first:
            first.started(live)
        live.return_value = Liveness.UNKNOWN
        first.release_if_absent(live)
        with self.assertRaises(SessionError), self.enter():
            pass
        live.return_value = Liveness.ABSENT
        self.assertTrue(first.finished.wait(5))
        with self.enter():
            pass

    def test_tui_record_survives_a_disjoint_host_configuration(self):
        first = self.enter(mode=Mode.TUI)
        with first:
            self.host.create("first", metadata={"client": "fake", "sid": "s1"})
            first.started(lambda: Liveness.LIVE)
        with (
            self.assertRaisesRegex(SessionError, "first"),
            self.enter(hosts={"elsewhere": MemoryHost("elsewhere")}),
        ):
            pass
        # Matching inventory reconciles an ended holder; a different sid is not the holder.
        self.host.processes["first"].calls.alive.return_value = False
        with self.enter(name="second", sid="s2"):
            pass

    def test_tui_reservation_is_durable_before_the_spawn(self):
        first = self.enter(mode=Mode.TUI)
        with first:
            records = list(Path(first.directory).glob("*.run"))
            self.assertEqual(len(records), 1)
            self.assertEqual(json.loads(records[0].read_text())["sid"], "s1")
            # Simulate a vanished launcher after spawn by retaining its pre-spawn reservation.
            first.started(lambda: Liveness.LIVE)
        with self.assertRaises(SessionError), self.enter(hosts={}):
            pass

    def test_inventory_counts_existing_done_blocked_unknown_but_not_dead(self):
        for state in ("working", "done", "blocked", "unknown", "dead", "gone"):
            with self.subTest(state=state):
                terminal = self.host.create("old", metadata={"client": "fake", "sid": "old"})
                terminal.calls.row.return_value = SessionRow(
                    "old", "old", "fake", state, None, None, None, own=False
                )
                if state in ("dead", "gone"):
                    with self.enter():
                        pass
                else:
                    with self.assertRaisesRegex(SessionError, "old"), self.enter():
                        pass
                terminal.close()

    def test_inventory_failure_or_missing_row_fails_closed(self):
        self.host.calls.list.side_effect = SessionError("offline")
        with self.assertRaisesRegex(SessionError, "offline"), self.enter():
            pass
        self.host.calls.list.side_effect = None
        self.host.calls.list.return_value = ["missing"]
        with self.assertRaises(SessionError), self.enter():
            pass

    def test_concurrent_admissions_serialize_the_count_and_start(self):
        barrier = threading.Barrier(2)
        admitted, refused, errors = [], [], []
        tickets = [self.enter(name=f"n{i}", sid=f"s{i}") for i in range(2)]

        def run(ticket):
            try:
                barrier.wait(5)
                with ticket:
                    ticket.started(lambda: Liveness.LIVE)
                    admitted.append(ticket)
            except SessionError as error:
                refused.append(str(error))
            except (AssertionError, OSError, RuntimeError) as error:
                errors.append(error)

        threads = [threading.Thread(target=run, args=(ticket,)) for ticket in tickets]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual((len(admitted), len(refused)), (1, 1))

    def test_stale_transient_file_is_reaped_and_symlinks_are_refused(self):
        first = self.enter()
        with first:
            first.started(lambda: Liveness.LIVE)
        directory = Path(first.directory)
        first.close()
        stale = directory / "old.run"
        stale.write_text('{"kind":"headless","name":"old","sid":"old"}')
        with self.enter():
            pass
        self.assertFalse(stale.exists())
        outside = Path(self.home) / "untouched"
        outside.write_text("unchanged")
        os.symlink(outside, stale)
        with self.assertRaises(SessionError), self.enter():
            pass
        self.assertEqual(outside.read_text(), "unchanged")

    def test_filesystem_error_is_a_session_error(self):
        with (
            mock.patch("os.makedirs", side_effect=PermissionError("denied")),
            self.assertRaisesRegex(SessionError, "client fake.*denied"),
            self.enter(),
        ):
            pass

    def test_invalid_or_nonregular_reservations_fail_closed(self):
        ticket = self.enter(limit=0)
        with ticket:
            pass
        path = Path(ticket.directory) / "bad.run"
        for content in ("not-json", "[]", "{}", '{"kind":"tui","sid":"s","name":"n"}'):
            with self.subTest(content=content):
                path.write_text(content)
                with self.assertRaises(SessionError), self.enter():
                    pass
        path.unlink()
        path.mkdir()
        with self.assertRaises(SessionError), self.enter():
            pass

    def test_replaced_tui_identity_is_reconciled_and_partial_identity_is_counted_once(self):
        first = self.enter(mode=Mode.TUI)
        with first:
            self.host.create("first", metadata={"client": "fake", "sid": "s1"})
            first.started(lambda: Liveness.LIVE)
        self.host.processes["first"].metadata["sid"] = None
        with self.enter(limit=2, name="second", sid="s2"):
            pass
        self.host.processes["first"].metadata["sid"] = "replacement"
        self.host.processes["first"].metadata["client"] = "other"
        with self.enter():
            pass
        self.assertFalse(Path(first.path).exists())

    def test_unknown_mode_probe_keeps_capacity_until_it_can_confirm_exit(self):
        ticket = self.enter()
        state = mock.Mock(
            side_effect=[SessionError("unknown"), OSError("offline"), Liveness.ABSENT]
        )
        with ticket:
            ticket.started(state)
        self.assertTrue(ticket.finished.wait(5))
        self.assertGreaterEqual(state.call_count, 3)
