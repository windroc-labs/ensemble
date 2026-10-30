"""Host and InteractiveProcess interfaces and defaults, attach targets, SIGNALS; Host.parse_hosts, the one [hosts]
parser.
"""

import dataclasses
import inspect
import json
import operator
import os
import re
import signal
import threading
import tomllib
from types import MappingProxyType
from unittest import mock

from ensemble.session import (
    SIGNALS,
    ConfigError,
    Host,
    NotShowable,
    SessionError,
)
from ensemble.session.engine.types import (
    LOCAL_CONTEXT,
    ClientContext,
    Hosts,
    Launch,
    SessionRow,
)
from ensemble.session.harness import guard
from ensemble.session.hosts import base as host_base
from ensemble.session.hosts.base import InteractiveProcess
from ensemble.session.hosts.types import CommandView
from ensemble.utils import Record, StatusState
from tests.session.case import Case
from tests.session.fakes.terminal import MemoryHost

TWO = """[hosts]
default = "a"

[hosts.a]
implementation = "pkg.mod:AHost"
per_column = 2

[hosts.b_2]
implementation = "pkg.b:BHost"
"""
NAMES = "a, b_2"
HIDDEN_ERROR = '[hosts] hidden_size: want "<columns>x<rows>", each 1 to 9999, e.g. "80x24"'
HIDDEN_INVALID = (
    *("0x24", "80x0", "-80x24", "80x-24", "80.5x24", "80x24.0", "80", "axb", " 80x24", "10000x24"),
    *(80, 80.5, None, True, [80, 24], {"columns": 80, "rows": 24}),
)

ABSTRACT = {
    Host: {"list"},
    InteractiveProcess: {"send", "press", "state", "close", "returncode"},
}


class MinimalHost(Host):
    def list(self):
        raise AssertionError("not called")


class MinimalHandle(InteractiveProcess):
    name, generation = "p", "g"

    def send(self, action):
        raise AssertionError("not called")

    press = state = close = returncode = send


class Abstract(Case):
    def test_each_interface_is_abstract_in_its_methods(self):
        for cls, methods in ABSTRACT.items():
            with self.subTest(cls=cls.__name__):
                self.assertTrue(inspect.isabstract(cls))
                self.assertEqual(cls.__abstractmethods__, methods)


class HostDefaults(Case):
    def test_a_host_hands_its_client_no_container(self):
        self.assertEqual(
            [f.name for f in dataclasses.fields(ClientContext)],
            ["host_address", "permission", "container"],
        )
        self.assertIsNone(MinimalHost("m")._client_context().container)
        self.assertIsNone(LOCAL_CONTEXT.container)

    def test_a_host_without_workspaces_removes_none(self):
        self.assertEqual(MinimalHost("m").remove_workspaces("/w/TASK-1/agent"), [])

    def test_a_handle_cannot_show_its_screen_unless_its_host_reads_one(self):
        for lines in (None, 5):
            with self.subTest(lines=lines), self.assertRaises(NotShowable) as caught:
                MinimalHandle().screen(lines)
            self.assertEqual(str(caught.exception), "session p: its host cannot show")

    def test_a_host_is_not_attachable_and_names_no_attach_target(self):
        host = MinimalHost("m")
        self.assertFalse(host.attachable)
        self.assertIsNone(host.attach_target("a"))
        self.assertEqual(CommandView("p", ("x",)), CommandView(printed="p", argv=("x",)))

    def test_name_and_a_copy_of_settings(self):
        settings = {"root": "/x"}
        host = MinimalHost("m", settings)
        self.assertEqual((host.name, host.settings), ("m", settings))
        self.assertIsNot(host.settings, settings)
        self.assertEqual(MinimalHost("m").settings, {})

    def test_isolated_is_the_hosts_own_bool_false_when_absent_and_never_in_settings(self):
        for settings, isolated in (
            ({}, False),
            ({"isolated": False}, False),
            ({"isolated": True, "root": "/x"}, True),
        ):
            with self.subTest(settings=settings):
                host = MinimalHost("m", settings)
                left = {k: v for k, v in settings.items() if k != "isolated"}
                self.assertEqual((host._isolated, host.settings), (isolated, left))
        for value in ("true", 1, None):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ConfigError, r"^\[hosts.m\] isolated: want true or false$"),
            ):
                MinimalHost("m", {"isolated": value})

    def test_hidden_size_is_the_hosts_own_80x24_when_absent_and_never_in_settings(self):
        for settings, size in (
            ({}, (80, 24)),
            ({"hidden_size": "100x30", "root": "/x"}, (100, 30)),
            ({"hidden_size": "9999x1"}, (9999, 1)),
        ):
            with self.subTest(settings=settings):
                host = MinimalHost("m", settings)
                left = {k: v for k, v in settings.items() if k != "hidden_size"}
                self.assertEqual((host.tui_settings().hidden_size, host.settings), (size, left))
        for value in HIDDEN_INVALID:
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ConfigError, f"^{re.escape(HIDDEN_ERROR)}$"),
            ):
                MinimalHost("m", {"hidden_size": value})

    def test_each_host_gets_its_own_mutable_settings_even_from_read_only_ones(self):
        given = MappingProxyType({"sizes": (1, [2]), "grid": MappingProxyType({"a": 1})})
        first, second = MinimalHost("a", given), MinimalHost("b", given)
        first.settings["sizes"][1].append(3)
        first.settings["grid"]["b"] = 2
        self.assertEqual(second.settings, {"sizes": (1, [2]), "grid": {"a": 1}})

    def test_signals_a_detached_run_blocks_are_hup_term_and_int(self):
        self.assertEqual(SIGNALS, (signal.SIGHUP, signal.SIGTERM, signal.SIGINT))


class CapacityRows(Case):
    """Admission's inventory: a row for every session listed before and after rows()."""

    def host(self, listings, rows):
        host = MinimalHost("m")
        host.list = mock.Mock(side_effect=listings)
        row = SessionRow("", None, "c", StatusState.WORKING, None, None, None, own=False)
        host.rows = mock.Mock(return_value=[dataclasses.replace(row, name=n) for n in rows])
        return host

    def test_a_session_ending_between_its_listing_and_rows_is_gone_not_unavailable(self):
        host = self.host([["a", "b"], ["a"]], ["a"])
        self.assertEqual([row.name for row in host._capacity_rows()], ["a"])

    def test_a_session_still_listed_without_a_row_makes_the_inventory_unavailable(self):
        host = self.host([["a", "b"], ["a", "b"]], ["a"])
        with self.assertRaisesRegex(SessionError, r"\Ahost m: inventory unavailable\Z"):
            host._capacity_rows()

    def test_a_complete_inventory_is_its_rows(self):
        host = self.host([["a", "b"]], ["b", "a"])
        self.assertEqual([row.name for row in host._capacity_rows()], ["b", "a"])

    def test_default_rows_are_each_listed_names_worker_state_and_activity(self):
        """sid, client and display from its session.json (none: None), state from its activity
        of that generation (none, or another generation's: working).
        """
        root = os.path.join(self.home, ".ensemble", "tui")
        for name, state, word in (
            ("w1", {"sid": "s1", "client": "c", "display": "mgr", "generation": "g1"}, "g1 done"),
            ("w2", {"sid": "s2", "client": "c", "generation": "g2"}, "g1 blocked"),
            ("w3", None, None),
        ):
            if state is not None:
                os.makedirs(os.path.join(root, name))
                with open(os.path.join(root, name, "session.json"), "w") as f:
                    json.dump(state, f)
                with open(os.path.join(root, name, "activity"), "w") as f:
                    f.write(word + "\n")
        host = MinimalHost("m")
        host.list = mock.Mock(return_value=["w1", "w2", "w3"])
        self.assertEqual(
            host._capacity_rows(),
            [
                SessionRow("w1", "s1", "c", StatusState.DONE, "mgr", None, None, own=False),
                SessionRow("w2", "s2", "c", StatusState.WORKING, None, None, None, own=False),
                SessionRow("w3", None, None, StatusState.WORKING, None, None, None, own=False),
            ],
        )


def table(base: str = TWO, text: str = ""):
    return tomllib.loads(base + text).get("hosts")


class ParseHosts(Case):
    def refused(self, found, message):
        with self.assertRaises(ConfigError) as caught:
            Host.parse_hosts(found)
        self.assertEqual(str(caught.exception), message)

    def test_the_default_and_its_tables(self):
        self.assertEqual(
            Host.parse_hosts(table()),
            Hosts(
                "a",
                {
                    "a": {"implementation": "pkg.mod:AHost", "per_column": 2},
                    "b_2": {"implementation": "pkg.b:BHost"},
                },
            ),
        )

    def test_the_tables_are_read_only_deep_copies(self):
        found = {**table(), "c": {"implementation": "pkg.c:CHost", "sizes": [1]}}
        hosts = Host.parse_hosts(found)
        found["a"]["per_column"] = 3
        found["c"]["sizes"].append(2)
        found["d"] = {"implementation": "pkg.d:DHost"}
        self.assertEqual((hosts.tables["a"]["per_column"], hosts.tables["c"]["sizes"]), (2, (1,)))
        self.assertNotIn("d", hosts.tables)
        for mapping, key in ((hosts.tables, "d"), (hosts.tables["a"], "per_column")):
            with self.subTest(key=key), self.assertRaises(TypeError):
                operator.setitem(mapping, key, 3)

    def test_a_malformed_hosts_table_is_refused_naming_the_part(self):
        names = (("A", "A"), ("1a", "1a"), ("a-b", "a-b"), ('"a b"', "a b"), ('""', ""))
        cases = (
            ({**table(), 1: {"implementation": "pkg.c:CHost"}}, "[hosts] 1: want a string key"),
            *((found, "[hosts]: missing") for found in (None, 1, "a", [])),
            (
                table(TWO.replace('default = "a"', 'default = "a"\nnoise = 1')),
                "[hosts] noise: want default or a [hosts.<name>] table",
            ),
            *(
                (
                    table(text=f'[hosts.{key}]\nimplementation = "pkg.c:CHost"\n'),
                    f"[hosts.{name}]: want a name matching [a-z][a-z0-9_]*",
                )
                for key, name in names
            ),
            *(
                (
                    table(TWO.replace('implementation = "pkg.mod:AHost"\n', line)),
                    "[hosts.a].implementation: want module.path:Class",
                )
                for line in ("", 'implementation = "pkg.mod.AHost"\n', "implementation = 1\n")
            ),
            *(
                (table(TWO.replace('default = "a"', line)), f"[hosts] default: want one of {NAMES}")
                for line in ("", "default = 1", 'default = ""', "default = []")
            ),
            (
                table(TWO.replace('default = "a"', 'default = "c"')),
                f"[hosts] default: 'c' is not a Host here; want one of {NAMES}",
            ),
        )
        for found, message in cases:
            with self.subTest(found=found, message=message):
                self.refused(found, message)

    def test_hidden_size_goes_into_every_hosts_table(self):
        hosts = Host.parse_hosts(
            table(TWO.replace('default = "a"', 'default = "a"\nhidden_size = "100x30"'))
        )
        self.assertEqual(
            {name: hosts.tables[name].get("hidden_size") for name in hosts.tables},
            {"a": "100x30", "b_2": "100x30"},
        )
        self.assertNotIn("hidden_size", Host.parse_hosts(table()).tables["a"])

    def test_an_invalid_hidden_size_or_one_in_a_hosts_table_is_refused(self):
        for value in HIDDEN_INVALID:
            with self.subTest(value=value):
                self.refused({**table(), "hidden_size": value}, HIDDEN_ERROR)
        self.refused(
            table(TWO.replace("per_column = 2", 'hidden_size = "80x24"')),
            "[hosts.a] hidden_size: set it in [hosts], for every Host",
        )

    def test_the_class_is_never_loaded(self):
        found = table(TWO.replace("pkg.mod:AHost", "ensemble.session.hosts.tmux:TmuxHost"))
        with mock.patch("importlib.import_module") as imp:
            self.assertEqual(Host.parse_hosts(found).default, "a")
        imp.assert_not_called()


class ManagedWorkerTest(Case):
    """The managed worker contract frozen for WDRC-199: no Host runs one yet."""

    def test_a_host_without_managed_workers_refuses_to_start_one(self):
        host = MemoryHost("h1")
        with self.assertRaisesRegex(SessionError, r"\Ahost h1 runs no managed worker\Z"):
            Host.worker(host, "w1", Launch(("client",), cwd="/w"), terminal=True)
        with self.assertRaisesRegex(SessionError, r"\Ahost h1 runs no managed worker\Z"):
            Host.worker_liveness(host, "w1", "g1")  # MemoryHost fakes its own
        self.assertEqual(host.calls.mock_calls, [])

    def test_a_worker_has_no_periodic_duty_unless_its_host_gives_one(self):
        self.assertIsNone(InteractiveProcess.tick(mock.sentinel.worker, 0.0))

    def test_channel_operations_remain_unimplemented(self):
        worker = mock.sentinel.worker
        for operation in (
            lambda: InteractiveProcess.read(worker, 0.0),
            lambda: InteractiveProcess.write(worker, b"input"),
        ):
            with self.subTest(operation=operation), self.assertRaises(NotImplementedError):
                operation()

    def test_the_guard_requires_an_explicit_client_command(self):
        with self.assertRaises(SystemExit) as caught:
            guard.main([])
        self.assertEqual(caught.exception.code, 2)


class WorkerStateFilesTest(Case):
    """worker_state, activity and stored_record read a worker dir's files only when regular, the
    user's, not a link and small; anything else reads as absent.
    """

    def setUp(self):
        self.dir = os.path.join(self.ensemble, "tui", "w1")
        os.makedirs(self.dir, 0o700)

    def put(self, name: str, data: bytes) -> str:
        path = os.path.join(self.dir, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_worker_state_from_session_json(self):
        self.put(
            "session.json",
            json.dumps(
                {"sid": "s", "generation": "g", "client": "fake", "display": "d", "owner": "/o"}
            ).encode(),
        )
        self.assertEqual(
            host_base.worker_state("w1"), host_base.WorkerState("s", "g", "fake", "d", "/o")
        )
        self.assertIsNone(host_base.worker_state("absent"))

    def test_activity_of_its_generation_only(self):
        self.put("activity", b"g done\n")
        self.assertEqual(host_base.activity("w1", "g"), StatusState.DONE)
        self.assertIsNone(host_base.activity("w1", "other"))
        self.put("activity", b"g dead\n")
        self.assertIsNone(host_base.activity("w1", "g"))

    def test_a_link_another_owner_or_a_large_file_reads_as_absent(self):
        target = self.put("elsewhere", b"g done\n")
        os.symlink(target, os.path.join(self.dir, "activity"))
        self.assertIsNone(host_base.activity("w1", "g"))
        os.unlink(os.path.join(self.dir, "activity"))
        self.put("activity", b"g done\n")
        with mock.patch("os.getuid", return_value=os.getuid() + 1):
            self.assertIsNone(host_base.activity("w1", "g"))
        self.put("activity", b"g done" + b" " * 300 + b"\n")
        self.assertIsNone(host_base.activity("w1", "g"))
        self.put("session.json", b"{" + b" " * (1 << 16) + b"}")
        self.assertIsNone(host_base.worker_state("w1"))

    def test_stored_record_of_its_generation_only_and_checked(self):
        record = Record(
            v=1,
            kind="worker",
            name="w1",
            sessions=("w1",),
            sid="s",
            workdir=None,
            ctl=("ensemble-session",),
            resume=("ensemble-session", "resume", "w1"),
            display=None,
            detail=None,
        )
        line = f"g\t{record.to_line()}\n".encode()
        self.put("record", line)
        self.assertEqual(host_base.stored_record("w1", "g"), record)
        self.assertIsNone(host_base.stored_record("w1", "other"))
        self.assertIsNone(host_base.stored_record("absent", "g"))
        for bad in (b"g " + line[2:], b"g\t{}\n", b"g\n"):
            self.put("record", bad)
            self.assertIsNone(host_base.stored_record("w1", "g"))
        target = self.put("elsewhere", line)
        os.unlink(os.path.join(self.dir, "record"))
        os.symlink(target, os.path.join(self.dir, "record"))
        self.assertIsNone(host_base.stored_record("w1", "g"))
        os.unlink(os.path.join(self.dir, "record"))
        self.put("record", line)
        with mock.patch("os.getuid", return_value=os.getuid() + 1):
            self.assertIsNone(host_base.stored_record("w1", "g"))
        self.put("record", line[:-1] + b" " * (1 << 16) + b"\n")
        self.assertIsNone(host_base.stored_record("w1", "g"))


class StartPhaseTest(Case):
    def test_a_phase_goes_to_this_contexts_reporter_and_ends_even_on_error(self):
        reports = []
        with host_base.phase_reporter(lambda phase, wait: reports.append((phase, wait))):
            with host_base.start_phase("building", 30):
                reports.append("inside")
            with self.assertRaises(RuntimeError), host_base.start_phase("building", 30):
                raise RuntimeError("broke")
        with host_base.start_phase("building", 30):
            pass
        self.assertEqual(
            reports, [("building", 30), "inside", (None, 0), ("building", 30), (None, 0)]
        )

    def test_a_thread_started_under_a_reporter_reports_to_none(self):
        reports = []

        def build():
            with host_base.start_phase("building", 30):
                pass

        with host_base.phase_reporter(lambda phase, wait: reports.append((phase, wait))):
            thread = threading.Thread(target=build)
            thread.start()
            thread.join()
        self.assertEqual(reports, [])
