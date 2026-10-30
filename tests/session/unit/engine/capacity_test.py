"""Client admission across configurations; filesystem locks, no processes or credentials."""

import fcntl
import itertools
import json
import os
import shutil
import threading
from pathlib import Path
from unittest import mock

from ensemble.session.engine import capacity
from ensemble.session.engine.capacity import Admission
from ensemble.session.engine.types import Liveness, Mode, SessionError, SessionRow
from ensemble.session.hosts.base import capacity_row
from tests.session.case import Case
from tests.session.fakes.terminal import MemoryHost

FIRST = {"client": "fake", "sid": "s1"}


class UnidentifiedHost(MemoryHost):
    """Its rows name no generation, as a Host may whose workers keep none."""

    def _capacity_rows(self):
        return [capacity_row(row, None) for row in self.rows()]


class SettlingHost(MemoryHost):
    """Runs `settle` on a thread once its rows are read, before the count's file scan."""

    settle = None

    def __init__(self, case):
        super().__init__()
        self.case = case

    def _capacity_rows(self):
        rows = super()._capacity_rows()
        if self.settle is not None:
            thread, self.settle = threading.Thread(target=self.settle, daemon=True), None
            thread.start()
            self.case.addCleanup(thread.join, 5)
            thread.join(0.5)
            self.settled = thread
        return rows


class AdmissionTest(Case):
    def setUp(self):
        self.host = MemoryHost()

    def enter(self, *, client="fake", limit=1, label="first", mode=Mode.HEADLESS, hosts=None):
        ticket = Admission(
            client,
            limit,
            label=label,
            mode=mode,
            hosts={"h": self.host} if hosts is None else hosts,
        )
        self.addCleanup(ticket.close)
        return ticket

    def promptly(self, action):
        """`action`'s SessionError, or None, once it returns on another thread within 2 s."""
        outcome = []

        def run():
            try:
                action()
                outcome.append(None)
            except SessionError as error:
                outcome.append(error)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        thread.join(2)
        self.assertTrue(outcome, "blocked behind another start of its client")
        return outcome[0]

    def admitted(self, ticket):
        def admit():
            with ticket:
                pass

        return self.promptly(admit)

    def test_a_start_in_flight_does_not_hold_back_other_starts_of_its_client(self):
        for limit in (0, 2):
            with self.subTest(limit=limit):
                first = self.enter(limit=limit, mode=Mode.TUI)
                with first:
                    self.assertIsNone(self.admitted(self.enter(limit=limit, label="second")))
                    self.assertIsNone(
                        self.promptly(lambda: Admission.reconcile("fake", {"h": self.host}))
                    )

    def test_an_in_flight_tui_start_holds_its_slot_before_its_session_exists(self):
        first = self.enter(mode=Mode.TUI)
        with first:
            error = self.admitted(self.enter(label="second"))
            self.assertRegex(str(error), "limit 1; held by: first")
            self.assertIsNone(self.promptly(lambda: Admission.reconcile("fake", {"h": self.host})))
            self.assertTrue(os.path.exists(first.path))
            self.host.create("first", metadata=FIRST | {"generation": first.generation})
            first.started(lambda: Liveness.LIVE)
        with self.assertRaisesRegex(SessionError, "held by: first"), self.enter(label="third"):
            self.fail("admitted")

    def test_an_in_flight_tui_start_and_its_session_count_once(self):
        first = self.enter(limit=2, mode=Mode.TUI)
        with first:
            self.host.create("first", metadata=FIRST | {"generation": first.generation})
            self.assertIsNone(self.admitted(self.enter(limit=2, label="second")))
            first.started(lambda: Liveness.LIVE)

    def test_a_tui_start_settling_during_a_count_is_still_counted(self):
        self.host = SettlingHost(self)
        first = self.enter(mode=Mode.TUI)

        def settle():
            self.host.create("first", metadata=FIRST | {"generation": first.generation})
            first.started(lambda: Liveness.LIVE)

        with first:
            self.host.settle = settle
            with (
                self.assertRaisesRegex(SessionError, "held by: first"),
                self.enter(label="second"),
            ):
                self.fail("admitted beside a live first")
            self.host.settled.join(5)
        self.assertTrue(os.path.exists(first.path))

    def test_a_failed_start_leaves_no_client_directory(self):
        root = os.path.dirname(self.enter(limit=0).directory)
        with self.assertRaisesRegex(ValueError, "failed"), self.enter():
            raise ValueError("failed")
        self.assertFalse(os.path.exists(root))
        first = self.enter()
        with first:
            first.started(lambda: Liveness.LIVE)
        with self.assertRaises(SessionError), self.enter(label="second"):
            pass
        first.close()
        self.assertTrue(os.path.exists(root), "a release from a watcher thread removes nothing")
        Admission.reconcile("fake", {"h": self.host})
        self.assertFalse(os.path.exists(root))

    def test_a_release_after_its_state_is_removed_creates_none_meanwhile(self):
        """A run's watcher may release after its home is being removed; it must not race that."""
        seen, unlink = [], os.unlink

        def observed(path, **kwargs):
            seen.append(os.path.exists(self.ensemble))
            unlink(path, **kwargs)

        for mode in (Mode.HEADLESS, Mode.TUI):
            with self.subTest(mode=mode):
                ticket = self.enter(mode=mode)
                with ticket:
                    ticket.started(lambda: Liveness.LIVE)
                shutil.rmtree(self.ensemble)
                seen.clear()
                with mock.patch.object(os, "unlink", observed):
                    ticket.close()
                self.assertTrue(seen, "the release removed no reservation")
                self.assertFalse(any(seen), "the release recreated its state")
                os.mkdir(self.ensemble)

    def test_live_transient_reservation_counts_and_failure_rolls_back(self):
        live = mock.Mock(return_value=Liveness.LIVE)
        first = self.enter()
        with first:
            first.started(live)
        with (
            self.assertRaisesRegex(SessionError, "client fake.*limit 1.*first"),
            self.enter(label="second"),
        ):
            self.fail("admitted")
        first.close()
        with self.assertRaisesRegex(ValueError, "failed"), self.enter():
            raise ValueError("failed")
        with self.enter():
            pass

    def test_unlimited_runs_are_counted_by_a_limited_caller_and_clients_are_separate(self):
        first = self.enter(limit=0, label="s1")
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
            self.host.create("first", metadata=FIRST | {"generation": first.generation})
            first.started(lambda: Liveness.LIVE)
        with (
            self.assertRaisesRegex(SessionError, "first"),
            self.enter(hosts={"elsewhere": MemoryHost("elsewhere")}),
        ):
            pass
        # Matching inventory reconciles an ended holder; a different sid is not the holder.
        self.host.processes["first"].calls.alive.return_value = False
        with self.enter(label="second"):
            pass

    def test_alias_configurations_count_one_run_by_generation(self):
        shared = MemoryHost()
        hosts = {"original-config": shared, "alias-config": shared}
        first = self.enter(limit=2, mode=Mode.TUI, hosts=hosts)
        with first:
            shared.create("first", metadata=FIRST | {"generation": first.generation})
            first.started(lambda: Liveness.LIVE)
        second = self.enter(limit=2, label="second", hosts=hosts)
        with second:
            second.started(lambda: Liveness.LIVE)
        with (
            self.assertRaisesRegex(SessionError, "held by: first, second"),
            self.enter(limit=2, label="third", hosts=hosts),
        ):
            self.fail("two real sessions must fill two slots")

    def test_independent_backends_with_equal_name_and_sid_count_separately(self):
        other = MemoryHost("other")
        for host in (self.host, other):
            host.create("same", metadata={"client": "fake", "sid": "same"})
        with (
            self.assertRaisesRegex(SessionError, "held by: same, same"),
            self.enter(limit=2, hosts={"a": self.host, "b": other}),
        ):
            self.fail("equal logical IDs do not identify a shared backend")

    def test_unknown_configuration_is_not_merged_by_matching_name_and_sid(self):
        first = self.enter(mode=Mode.TUI, hosts={"old-config": MemoryHost()})
        with first:
            first.started(lambda: Liveness.LIVE)
        other = MemoryHost()
        other.create("first", metadata=FIRST)  # another run of equal name and sid
        with (
            self.assertRaisesRegex(SessionError, "held by: first, first"),
            self.enter(limit=2, hosts={"new-config": other}),
        ):
            self.fail("an unknown reservation must retain its separate slot")

    def test_reconciliation_cleans_known_aliases_but_retains_unavailable_other_hosts(self):
        closed = self.enter(mode=Mode.TUI, hosts={"closed": self.host})
        with closed:
            closed.started(lambda: Liveness.LIVE)
        other = MemoryHost("offline")
        unknown = self.enter(mode=Mode.TUI, hosts={"offline": other}, limit=0)
        with unknown:
            unknown.started(lambda: Liveness.LIVE)
        other.calls.list.side_effect = SessionError("offline")
        hosts = {"closed": self.host, "offline": other}
        Admission.reconcile("fake", hosts, required="closed")
        self.assertFalse(os.path.exists(closed.path))
        self.assertTrue(os.path.exists(unknown.path))
        with self.assertRaisesRegex(SessionError, "offline"):
            Admission.reconcile("fake", hosts, required="offline")
        with self.assertRaisesRegex(SessionError, "offline"), self.enter(hosts=hosts, limit=10):
            self.fail("admission must still require every configured inventory")

    def test_tui_reservation_is_durable_before_the_spawn(self):
        first = self.enter(mode=Mode.TUI)
        with first:
            records = list(Path(first.directory).glob("*.run"))
            self.assertEqual([record.name for record in records], [f"{first.generation}.run"])
            self.assertEqual(
                json.loads(records[0].read_text()), {"kind": "tui", "name": "first", "host": "h"}
            )
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

    def test_admission_succeeds_when_a_listed_session_ends_before_attach(self):
        ended = self.host.create("old", metadata={"client": "fake", "sid": "old"})
        self.host.calls.process.side_effect = lambda _name: ended.close()
        with self.enter():
            self.assertEqual(self.host.list(), [])
        ended.calls.close.assert_called_once_with()

    def test_concurrent_admissions_serialize_the_count_and_reservation(self):
        barrier = threading.Barrier(2)
        admitted, refused, errors = [], [], []
        tickets = [self.enter(label=f"n{i}") for i in range(2)]

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
        directory.mkdir(parents=True, exist_ok=True)
        stale.write_text('{"kind":"headless","name":"old","sid":"old"}')
        with self.enter():
            pass
        self.assertFalse(stale.exists())
        outside = Path(self.home) / "untouched"
        outside.write_text("unchanged")
        stale.parent.mkdir(parents=True, exist_ok=True)
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
        path.parent.mkdir(parents=True, exist_ok=True)
        for content in ("not-json", "[]", "{}", '{"kind":"tui","sid":"s","name":"n"}'):
            with self.subTest(content=content):
                path.write_text(content)
                with self.assertRaises(SessionError), self.enter():
                    pass
        path.unlink()
        path.mkdir()
        with self.assertRaises(SessionError), self.enter():
            pass

    def test_replaced_tui_generation_is_reconciled(self):
        first = self.enter(mode=Mode.TUI)
        with first:
            self.host.create("first", metadata=FIRST | {"generation": first.generation})
            first.started(lambda: Liveness.LIVE)
        self.host.create("first", metadata=FIRST)  # its next worker, of another generation
        with self.assertRaisesRegex(SessionError, "held by: first$"), self.enter(label="second"):
            self.fail("the replacement counts once")
        self.assertFalse(Path(first.path).exists())

    def test_earlier_format_reservations_count_by_stem_or_are_reaped(self):
        directory = Path(self.enter(limit=0).directory)
        directory.mkdir(parents=True)
        held = directory / "0123.run"
        held.write_text('{"kind": "headless", "name": "old", "sid": "s0"}')
        lease = os.open(held, os.O_RDONLY)
        self.addCleanup(os.close, lease)
        fcntl.flock(lease, fcntl.LOCK_EX)
        detached = directory / "4567.run"
        detached.write_text('{"kind": "tui", "name": "older", "sid": "s1", "host": "h"}')
        self.host.create("older", metadata=FIRST)
        with (
            self.assertRaisesRegex(SessionError, "limit 2; held by: old, older$"),
            self.enter(limit=2, label="new"),
        ):
            self.fail("the held earlier file and the row count, the detached file does not")
        self.assertTrue(held.exists())
        self.assertFalse(detached.exists())

    def test_a_live_row_without_generation_counts_once(self):
        self.host = UnidentifiedHost()
        self.host.create("bare", metadata=FIRST)
        with self.assertRaisesRegex(SessionError, "limit 1; held by: bare$"), self.enter():
            self.fail("an unidentified live row holds its slot")
        with self.enter(limit=2):
            pass

    def test_unknown_mode_probe_keeps_capacity_until_it_can_confirm_exit(self):
        ticket = self.enter()
        state = mock.Mock(
            side_effect=[SessionError("unknown"), OSError("offline"), Liveness.ABSENT]
        )
        with ticket:
            ticket.started(state)
        self.assertTrue(ticket.finished.wait(5))
        self.assertGreaterEqual(state.call_count, 3)


class IdentityCountTest(Case):
    """Each live run counts once, by its generation, whatever pairs a guess could confuse."""

    CASES = tuple(
        itertools.product(
            (Mode.TUI, Mode.HEADLESS),
            (True, False),  # named
            (True, False),  # another live run shares its sid
            ("live", "absent", "dead"),  # its Host row
            ("kept", "released"),  # its reservation
            (False, True),  # reservations scanned in reverse
        )
    )

    def run_of(self, client, hosts, host, *, mode, named, row, reservation, name):
        """A started run of `client` on `host`, with its row and reservation as given; whether
        it is live.
        """
        sid = "shared"
        label = name if named or mode == Mode.TUI else sid
        ticket = Admission(client, 0, label=label, mode=mode, hosts=hosts, host=host)
        self.addCleanup(ticket.close)
        with ticket:
            if row != "absent":
                worker = name if named or mode == Mode.TUI else f"headless-{ticket.generation}"
                metadata = {"client": client, "sid": sid, "generation": ticket.generation}
                hosts[host].create(worker, metadata=metadata)
                hosts[host].processes[worker].calls.alive.return_value = row == "live"
            ticket.started(lambda: Liveness.LIVE)
        if reservation == "released":
            ticket.close()
        kept = reservation == "kept" and (mode == Mode.HEADLESS or row == "live")
        return kept or row == "live"

    def count_is(self, client, hosts, expected, reverse):
        scan = os.scandir

        def ordered(path):
            entries = sorted(scan(path), key=lambda entry: entry.name)
            return iter(entries[::-1] if reverse else entries)

        probe = {"label": "probe", "mode": Mode.HEADLESS, "hosts": hosts}
        with mock.patch.object(capacity.os, "scandir", ordered):
            if expected:
                with (
                    self.assertRaisesRegex(SessionError, f"limit {expected}; held by"),
                    Admission(client, expected, **probe),
                ):
                    self.fail(f"admitted beside {expected} live runs")
            with Admission(client, expected + 1, **probe):
                pass

    def test_every_live_run_counts_once(self):
        for index, (mode, named, shared, row, reservation, reverse) in enumerate(self.CASES):
            with self.subTest(
                mode=mode.value,
                named=named,
                shared=shared,
                row=row,
                reservation=reservation,
                reverse=reverse,
            ):
                client = f"c{index}"
                hosts = {"h": MemoryHost("h"), "other": MemoryHost("other")}
                live = self.run_of(
                    client,
                    hosts,
                    "h",
                    mode=mode,
                    named=named,
                    row=row,
                    reservation=reservation,
                    name="a",
                )
                if shared:
                    self.run_of(
                        client,
                        hosts,
                        "other",
                        mode=mode,
                        named=named,
                        row="live",
                        reservation="kept",
                        name="b",
                    )
                self.count_is(client, hosts, int(live) + int(shared), reverse)
