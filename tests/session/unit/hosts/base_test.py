"""Host process interfaces and defaults, attach targets, SIGNALS; Host.parse_hosts, the one [hosts]
parser.
"""

import dataclasses
import inspect
import operator
import signal
import tomllib
from types import MappingProxyType
from unittest import mock

from ensemble.session import (
    SIGNALS,
    ConfigError,
    Host,
    NotShowable,
    Process,
    SessionError,
)
from ensemble.session.engine.types import (
    LOCAL_CONTEXT,
    ClientContext,
    Hosts,
    Launch,
    Liveness,
    SessionRow,
)
from ensemble.session.harness import guard
from ensemble.session.hosts.base import Interactive, InteractiveProcess, Piped
from ensemble.session.hosts.types import CommandView, SessionView
from ensemble.utils import STOP_WAIT, Status, StatusState
from tests.session.case import Case
from tests.session.fakes.terminal import MemoryHost
from tests.utils.process_fixtures import DEVNULL, PIPE, TimeoutExpired

TWO = """[hosts]
default = "a"

[hosts.a]
implementation = "pkg.mod:AHost"
per_column = 2

[hosts.b_2]
implementation = "pkg.b:BHost"
"""
NAMES = "a, b_2"

PROCESS = {"alive", "close", "wait_exit", "kill", "record", "display"}

ABSTRACT = {
    Host: {"detach", "terminal", "attach", "list"},
    Process: PROCESS,
    Interactive: {"send", "press"},
    InteractiveProcess: PROCESS | {"send", "press"},
    Piped: {"wait", "stop"},
}


class MinimalHost(Host):
    def detach(self, *args, **kwargs):
        raise AssertionError("not called")

    terminal = attach = list = detach


class Abstract(Case):
    def test_each_interface_is_abstract_in_its_methods(self):
        for cls, methods in ABSTRACT.items():
            with self.subTest(cls=cls.__name__):
                self.assertTrue(inspect.isabstract(cls))
                self.assertEqual(cls.__abstractmethods__, methods)

    def test_an_interactive_process_is_a_process_and_interactive(self):
        self.assertTrue(issubclass(InteractiveProcess, Process))
        self.assertTrue(issubclass(InteractiveProcess, Interactive))

    def test_list_rows_and_validate_take_no_display(self):
        for method, parameters in (
            (Host.list, ["self"]),
            (Host.rows, ["self"]),
            (Host.validate, ["self", "name", "launch"]),
        ):
            with self.subTest(method=method.__name__):
                found = inspect.signature(method).parameters
                self.assertEqual(list(found), parameters)


class HostDefaults(Case):
    def test_a_host_hands_its_client_no_container(self):
        self.assertEqual(
            [f.name for f in dataclasses.fields(ClientContext)],
            ["host_address", "permission", "container"],
        )
        self.assertIsNone(MinimalHost("m")._client_context().container)
        self.assertIsNone(LOCAL_CONTEXT.container)

    def test_a_host_reclaims_nothing_and_a_process_neither_publishes_nor_has_a_token(self):
        self.assertIsNone(MinimalHost("m").reclaim("/tmp/apm-group-x"))
        process = mock.create_autospec(Process, instance=True)
        process.name = "p"
        self.assertIsNone(Process.token.fget(process))
        with self.assertRaisesRegex(SessionError, "^session p: its host cannot store a Record"):
            Process.publish(process, mock.sentinel.record)

    def test_a_process_cannot_show_its_screen_unless_its_terminal_reads_one(self):
        process = mock.create_autospec(Process, instance=True)
        process.name = "p"
        for lines in (None, 5):
            with self.subTest(lines=lines), self.assertRaises(NotShowable) as caught:
                Process.screen(process, lines)
            self.assertEqual(str(caught.exception), "session p: its host cannot show")

    def test_a_process_status_is_its_liveness_and_activity(self):
        process = mock.create_autospec(Process, instance=True)
        process.name = "p"
        for liveness, alive, activity, state in (
            (Liveness.LIVE, True, None, StatusState.WORKING),
            (Liveness.LIVE, True, StatusState.BLOCKED, StatusState.BLOCKED),
            (Liveness.LIVE, False, None, StatusState.DEAD),
            (Liveness.ABSENT, False, None, StatusState.DEAD),
            (Liveness.UNKNOWN, False, None, StatusState.UNKNOWN),
        ):
            with self.subTest(liveness=liveness, alive=alive, activity=activity):
                process.state.return_value, process.alive.return_value = liveness, alive
                process.activity.return_value = activity
                self.assertEqual(
                    Process.status(process),
                    Status(
                        name="p",
                        alive=alive,
                        state=state,
                        outcome=None,
                        attach=None,
                        hidden=False,
                    ),
                )

    def test_a_host_failing_after_telling_liveness_is_unknown(self):
        process = mock.create_autospec(Process, instance=True)
        process.name, process.state.return_value = "p", Liveness.LIVE
        for failing in (process.alive, process.activity):
            with self.subTest(failing=failing):
                process.reset_mock()
                process.state.return_value, process.alive.return_value = Liveness.LIVE, True
                failing.side_effect = SessionError("tmux failed")
                found = Process.status(process)
                self.assertEqual(
                    (found.alive, found.state, found.hidden), (False, "unknown", False)
                )

    def test_a_host_is_not_attachable_and_names_no_attach_target(self):
        host = MinimalHost("m")
        self.assertFalse(host.attachable)
        self.assertIsNone(host.attach_target("a"))
        self.assertEqual(SessionView("a"), SessionView(session="a"))
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

    def test_default_rows_tolerate_a_session_ending_before_attach_or_row(self):
        for boundary in ("attach", "row"):
            with self.subTest(boundary=boundary):
                host = MinimalHost("m")
                host.list = mock.Mock(
                    side_effect=[["ended", "live"], ["ended", "live"], ["live"], ["live"]]
                )
                live = SessionRow(
                    "live", None, "c", StatusState.WORKING, None, None, None, own=False
                )
                process = mock.Mock()
                process.row.return_value = live
                ended = mock.Mock()
                ended.row.side_effect = SessionError("ended")
                host.attach = mock.Mock(
                    side_effect=[SessionError("ended") if boundary == "attach" else ended, process]
                )
                self.assertEqual(host._capacity_rows(), [live])

    def test_default_rows_preserve_read_failures_for_sessions_still_listed(self):
        host = MinimalHost("m")
        host.list = mock.Mock(return_value=["live"])
        host.attach = mock.Mock(side_effect=SessionError("cannot read"))
        with self.assertRaisesRegex(SessionError, "cannot read"):
            host._capacity_rows()

    def test_default_rows_fail_closed_when_absence_cannot_be_confirmed(self):
        host = MinimalHost("m")
        host.list = mock.Mock(side_effect=[["live"], ["live"], SessionError("cannot list")])
        host.attach = mock.Mock(side_effect=SessionError("cannot read"))
        with self.assertRaisesRegex(SessionError, "cannot list"):
            host._capacity_rows()


def table(base: str = TWO, text: str = ""):
    return tomllib.loads(base + text).get("hosts")


class Pipe(Case):
    def test_the_base_pipe_starts_the_launch_here_on_pipes_in_its_own_group(self):
        launch = Launch(("cli", "-p"), env={"K": "v"}, cwd="/w")
        proc = mock.Mock(stdout=object(), stderr=object())
        with mock.patch("subprocess.Popen", return_value=proc) as popen:
            piped = MinimalHost("m").pipe(None, launch, metadata={"sid": "s"})
        popen.assert_called_once_with(
            ("cli", "-p"),
            cwd="/w",
            env={"K": "v"},
            stdin=DEVNULL,
            stdout=PIPE,
            stderr=PIPE,
            text=True,
            errors="replace",
            process_group=0,
        )
        self.assertEqual((piped.stdout, piped.stderr), (proc.stdout, proc.stderr))

    def test_a_local_piped_waits_within_its_timeout_and_stop_kills_its_group(self):
        proc = mock.Mock()
        proc.wait.side_effect = [TimeoutExpired("cli", 0), 3, 3]
        with mock.patch("subprocess.Popen", return_value=proc):
            piped = MinimalHost("m").pipe(None, Launch(("cli",), cwd="/w"))
        self.assertIsNone(piped.wait(0))
        self.assertEqual(piped.wait(2), 3)
        self.assertEqual(
            [c.kwargs for c in proc.wait.call_args_list], [{"timeout": 0}, {"timeout": 2}]
        )
        proc.pid = 4321
        proc.returncode = None
        with mock.patch("os.killpg") as killpg:
            piped.stop(ended=True)
            piped.close()
        killpg.assert_called_once_with(4321, signal.SIGKILL)
        self.assertEqual(proc.wait.call_args.kwargs, {"timeout": STOP_WAIT})


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

    def test_managed_workers_reuse_existing_input_and_lifecycle_members(self):
        for name in ("send", "press"):
            self.assertIs(getattr(InteractiveProcess, name), getattr(Interactive, name))
        for name in ("screen", "state", "close", "returncode"):
            self.assertIs(getattr(InteractiveProcess, name), getattr(Process, name))

    def test_the_guard_requires_an_explicit_client_command(self):
        with self.assertRaises(SystemExit) as caught:
            guard.main([])
        self.assertEqual(caught.exception.code, 2)
