"""Session behavior tests with caller-owned dependencies."""

import contextlib
import functools
import io
import os
import re
import subprocess
import time
from unittest import mock

from ensemble.session.engine.tmux_cli import TmuxCli
from ensemble.session.engine.types import Launch, SessionError
from ensemble.session.hosts.tmux import TmuxHost
from ensemble.session.viewer import grid as tmux_model
from ensemble.session.viewer import layout
from ensemble.session.viewer.viewer import fold
from tests.session import live_tmux
from tests.session.case import PLUGIN_BIN, Case, configured
from tests.utils.sandbox import scratch

MANAGER = "mgr"

TIMEOUT = 60

COMMAND = os.path.join(PLUGIN_BIN, "ensemble-session")

IGNORED = "tui: show: grid: --split/--split-from ignored"

STEADY = 1  # seconds a check that nothing re-tiles holds

BREAKER_TIMEOUT = 30


class Live(Case):
    def setUp(self):
        self.server = live_tmux.Server(self, self.home)
        self.server.start(MANAGER)
        self.server.attach(MANAGER)
        patch = mock.patch.dict(os.environ, self.server.env(), clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.cwd = scratch(self)
        self.host = TmuxHost("tmux")
        self.workers = {}
        self.settings = {}
        self.configure()

    def configure(self, **settings) -> None:
        """The [viewer] settings show() reads: these, over the earlier ones."""
        self.settings.update(settings)
        self.session_cls = configured({}, viewer=self.settings)

    def per_column(self, n: int) -> None:
        self.configure(workers_per_column=n)

    def retile(self, mode: str) -> None:
        self.configure(grid_retile=mode)

    @contextlib.contextmanager
    def inside(self, session: str | None):
        """os.environ as a process's in `session`'s pane; None: outside tmux."""
        with mock.patch.dict(os.environ, self.server.inside(session) if session else {}):
            yield

    def show(self, name: str, inside: str | None = MANAGER, split=None, split_from=None) -> str:
        """The Viewer's show(name) on the default Host at split and split_from from inside `inside`, a pane then
        showing it; its stderr.
        """
        err = io.StringIO()
        display = fold(None, split, split_from)
        host = self.session_cls.host()
        if not host.attachable:
            self.fail(f"host {host.name} cannot show")
        with self.inside(inside), contextlib.redirect_stderr(err):
            self.assertIsNone(self.session_cls.viewer().show(name, host, display=display))
        return err.getvalue()

    def started(self, name: str, inside: str | None = MANAGER, **kwargs) -> str:
        """tmux session `name` started with no pane, then show()n; show's stderr."""
        self.worker(name)
        return self.show(name, inside, **kwargs)

    def worker(self, name):
        handle = self.host.worker(
            name, Launch(live_tmux.IDLE, self.server.env(), cwd=self.cwd), terminal=True
        )
        self.workers[name] = handle
        self.addCleanup(self.cleanup_worker, name, handle)
        self.session_cls.viewer()._prepare(name, self.host)
        return handle

    def cleanup_worker(self, name, handle):
        with contextlib.suppress(SessionError):
            handle.close()
        if self.workers.get(name) is handle:
            self.workers.pop(name)
            self.session_cls.viewer().close(name, self.host)

    def close(self, name: str) -> None:
        if name in self.workers:
            self.cleanup_worker(name, self.workers[name])
        else:
            TmuxCli().kill(name)

    def tmux(self, *args: str) -> str:
        res = self.server.tmux(*args)
        self.assertEqual(res.returncode, 0, f"tmux {args}: {res.stderr}")
        return res.stdout.removesuffix("\n")

    def tui(self, *args: str) -> None:
        """ensemble-session, run from inside the manager's pane."""
        res = subprocess.run(
            [COMMAND, *args],
            env=self.server.env(**self.server.inside(MANAGER)),
            cwd=self.cwd,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=TIMEOUT,
        )
        self.assertEqual(res.returncode, 0, res.stderr)

    def display(self, name: str, fmt: str) -> str:
        return self.tmux("display-message", "-p", "-t", f"={name}:", fmt)

    def window_option(self, name: str, key: str) -> str:
        return self.tmux("show-options", "-wqv", "-t", f"={name}:", key)

    def window_of(self, name: str) -> str:
        return self.display(name, "#{window_id}")

    def grid(self, columns: list, manager_width: int | None = None) -> None:
        check = functools.partial(
            live_tmux.assert_grid, self, self.server, MANAGER, columns, manager_width
        )
        with contextlib.suppress(
            AssertionError
        ):  # on timeout, check's own message below rather than wait's repr
            live_tmux.wait(check)
        check()

    def hooks(self, name: str) -> list[str]:
        out = self.tmux("show-hooks", "-w", "-t", self.window_of(name))
        return [h for h in tmux_model.GRID_HOOKS if re.search(rf"^{h}\b", out, re.M)]

    def open_close_hooks(self, name: str) -> None:
        self.assertEqual(self.hooks(name), list(tmux_model.RETILE_HOOKS["open-close"]))
        out = self.tmux("show-hooks", "-w", "-t", self.window_of(name))
        for h in tmux_model.RETILE_HOOKS["open-close"]:
            line = next(line for line in out.splitlines() if re.match(rf"{h}\b", line))
            self.assertIn(" tile --no-wait --if-panes-changed @", line)

    def pane_set(self, name: str) -> str:
        panes = self.tmux("list-panes", "-t", self.window_of(name), "-F", "#{pane_id}").split()
        return " ".join(sorted(panes, key=lambda p: int(p[1:])))

    def viewer_settled(self, name: str) -> None:
        """Waits until name's viewer client, the tmux client in its grid pane, has the pane's size.
        Its resize lands after a re-tile and makes it the window's latest client: with tmux's
        window-size latest, a client attached earlier then no longer sizes the window.
        """
        pane = self.tmux(
            "display-message",
            "-p",
            "-t",
            self.server.pane_of(name),
            "#{pane_tty} #{pane_width}x#{pane_height}",
        )
        clients = functools.partial(
            self.tmux,
            "list-clients",
            "-t",
            f"={name}",
            "-F",
            "#{client_tty} #{client_width}x#{client_height}",
        )
        live_tmux.wait(
            lambda: pane in clients().splitlines(),
            what=f"{name}'s viewer client at its pane's size ({pane})",
        )

    def holds(self, read, value, what: str):
        end = time.monotonic() + STEADY
        while time.monotonic() < end:
            self.assertEqual(read(), value, what)
            time.sleep(live_tmux.POLL)
        return value


class GridTest(Live):
    def setUp(self):
        super().setUp()
        self.retile("all")

    def test_columns_retile(self):
        self.per_column(2)
        # tmux's split leaves the split pane the wider half (100 of 200); tile keeps the manager's
        # width
        width = self.server.size[0] // 2
        self.started("w1")
        self.grid([["w1"]], width)
        self.assertEqual(self.window_option(MANAGER, "@grid-per-column"), "2")
        self.assertIn(IGNORED, self.started("w2", split="below").splitlines())
        self.grid([["w1", "w2"]], width)
        self.started("w3")
        self.grid([["w1", "w2"], ["w3"]], width)
        self.close("w2")
        self.grid([["w1", "w3"]], width)

    def test_sub_sessions(self):
        for i, name in enumerate(("m1", "m2", "m3"), 1):
            self.started(name)
            self.grid([[f"m{k}" for k in range(1, i + 1)]])
        self.started("z1", inside="m2")
        self.grid([["m1", ("m2", ["z1"]), "m3"]])
        self.started("z2", inside="m2")
        self.grid([["m1", ("m2", ["z1", "z2"]), "m3"]])
        self.close("z1")
        self.close("z2")
        self.grid([["m1", "m2", "m3"]])
        self.started("z3", inside="m2")
        self.grid([["m1", ("m2", ["z3"]), "m3"]])
        self.close("m2")
        self.grid([["m1", ["z3"], "m3"]])

    def test_an_unshown_session_reopens_in_the_grid(self):
        self.started("w1")
        self.grid([["w1"]])
        closed = self.server.pane_of("w1")
        self.tmux("kill-pane", "-t", closed)
        live_tmux.wait(
            lambda: not self.tmux("list-clients", "-t", "=w1"), what="w1 shown by no client"
        )
        self.assertEqual(self.show("w1"), "")
        self.assertNotEqual(self.server.pane_of("w1"), closed)
        self.grid([["w1"]])
        self.assertEqual(self.server.option("w1", "@opener"), MANAGER)


class SplitsTest(Live):
    def test_show_outside_tmux_returns_attach_even_with_an_anchor(self):
        with self.inside(MANAGER):
            self.assertEqual(self.session_cls.layout(), MANAGER)
        beside = f"/below/{MANAGER}"
        self.assertEqual(self.session_cls.layout(display=beside), beside)
        with self.assertRaises(SessionError) as caught:
            self.session_cls.layout(display="/below")
        self.assertTrue(
            str(caught.exception).endswith(": run from tmux, or pass split_from"),
            caught.exception,
        )
        before = self.server.panes(f"={MANAGER}:")
        self.worker("w1")
        self.assertEqual(
            self.session_cls.viewer().show("w1", self.host, display=beside),
            self.host.attach_target("w1").printed,
        )
        self.assertEqual(self.server.panes(f"={MANAGER}:"), before)
        self.assertEqual(self.server.option("w1", "@pane"), "")
        self.assertEqual(
            [self.window_option(MANAGER, k) for k in ("@grid-manager", "@grid-per-column")],
            ["", ""],
        )


class RetileTest(Live):
    def setUp(self):
        super().setUp()
        self.manager = self.server.inside(MANAGER)["TMUX_PANE"]

    def panes(self) -> dict[str, tuple[int, int, int, int]]:
        return self.server.panes(f"={MANAGER}:")

    def layout(self) -> str:
        return self.display(MANAGER, "#{window_layout}")

    def tiles(self) -> str:
        return self.window_option(MANAGER, tmux_model.GRID_TILES)

    def drag(self, name: str, rows: int) -> str:
        before = self.layout()
        self.tmux(
            "resize-pane",
            "-t",
            self.server.pane_of(name),
            "-D" if rows > 0 else "-U",
            str(abs(rows)),
        )
        after = self.layout()
        self.assertNotEqual(after, before)
        return after

    def split_of(self, before: dict, name: str, target: str, side: str) -> None:
        after, new = self.panes(), self.server.pane_of(name)
        self.assertEqual(set(after), {*before, new})
        self.assertEqual(
            {p: after[p] for p in before if p != target},
            {p: before[p] for p in before if p != target},
        )
        (x, y, w, h), kept, added = before[target], after[target], after[new]
        if side == "right":
            want = (x, y, kept[2], h), (x + kept[2] + 1, y, w - kept[2] - 1, h)
        else:
            want = (x, y, w, kept[3]), (x, y + kept[3] + 1, w, h - kept[3] - 1)
        self.assertEqual((kept, added), want, f"{name}: {side} of {target} ({before[target]})")
        self.assertTrue(min(*kept[2:], *added[2:]) > 0, (kept, added))

    def test_open_close_retiles_on_an_open_only(self):
        width = self.server.size[0] // 2
        self.started("w1")
        self.started("w2")
        self.grid([["w1", "w2"]], width)
        self.open_close_hooks(MANAGER)
        self.assertEqual(
            self.window_option(MANAGER, tmux_model.GRID_PANES_SET), self.pane_set(MANAGER)
        )
        dragged = self.drag("w1", 5)
        self.holds(self.layout, dragged, "the layout after a drag")
        tiles = self.tiles()
        self.tmux("resize-window", "-t", self.window_of(MANAGER), "-x", "160", "-y", "40")
        self.assertEqual(self.display(MANAGER, "#{window_width}x#{window_height}"), "160x40")
        heights = [self.panes()[self.server.pane_of(n)][3] for n in ("w1", "w2")]
        self.assertGreater(heights[0] - heights[1], 1)
        self.holds(
            lambda: (self.layout(), self.tiles()),
            (self.layout(), tiles),
            "the layout and @grid-tiles after a window resize",
        )
        width = self.panes()[self.manager][2]
        self.started("w3")
        self.grid([["w1", "w2", "w3"]], width)
        self.open_close_hooks(MANAGER)
        self.holds(self.layout, self.layout(), "the layout after an open")

    def test_open_close_retiles_on_a_close(self):
        width = self.server.size[0] // 2
        for name in ("w1", "w2", "w3"):
            self.started(name)
        self.grid([["w1", "w2", "w3"]], width)
        # the most recently active client shows w1's session, not mgr's: tmux 3.4 then skips the
        # window's pane-exited hook, so window-layout-changed (a new pane set) re-tiles
        self.viewer_settled("w1")
        self.server.attach("w1")
        self.close("w2")
        self.grid([["w1", "w3"]], width)
        self.open_close_hooks(MANAGER)
        self.close("w3")
        self.grid([["w1"]], width)
        live_tmux.wait(
            lambda: (
                self.window_option(MANAGER, tmux_model.GRID_PANES_SET) == self.pane_set(MANAGER)
            ),
            what="@grid-panes the window's pane set",
        )

    def test_off_places_only_till_tile(self):
        self.retile("off")
        before = self.panes()
        self.started("w1")
        w1 = self.server.pane_of("w1")
        self.split_of(before, "w1", self.manager, "right")
        before = self.panes()
        self.started("w2")
        self.split_of(before, "w2", w1, "below")
        self.drag("w1", -10)
        before = self.panes()
        largest = max(
            (p for p in before if p != self.manager),
            key=lambda p: (before[p][2] * before[p][3], int(p[1:])),
        )
        self.assertEqual(largest, self.server.pane_of("w2"))
        self.started("z1", inside="w1")
        self.split_of(before, "z1", largest, "below")
        self.holds(self.panes, self.panes(), "the panes as placed")
        self.assertEqual(self.hooks(MANAGER), [])
        options = (tmux_model.GRID_MANAGER, tmux_model.GRID_PER_COLUMN)
        self.assertEqual(
            [self.window_option(MANAGER, k) for k in options],
            [self.manager, str(tmux_model.PER_COLUMN)],
        )
        self.tui("tile", self.window_of(MANAGER))
        self.grid([[("w1", ["z1"]), "w2"]], self.server.size[0] // 2)

    def test_a_switch_to_open_close_leaves_only_its_hooks(self):
        self.retile("all")
        self.started("w1")
        self.grid([["w1"]])
        self.assertEqual(self.hooks(MANAGER), list(tmux_model.GRID_HOOKS))
        self.retile("open-close")
        self.started("w2")
        self.grid([["w1", "w2"]])
        self.open_close_hooks(MANAGER)
        dragged = self.drag("w1", 5)
        self.holds(self.layout, dragged, "the layout after a drag")


class BoundaryTest(Live):
    def setUp(self):
        super().setUp()
        self.retile("off")
        self.started("w1")
        self.started("w2")
        self.manager = self.server.inside(MANAGER)["TMUX_PANE"]
        self.window = self.window_of(MANAGER)
        self.tui("tile", self.window)

    def layout(self):
        return self.display(MANAGER, "#{window_layout}")

    def assert_complete(self):
        actual = set(self.server.panes(self.window))
        leaves = layout.pane_ids(layout.parse_layout(self.layout()))
        self.assertEqual(set(leaves), actual)
        self.assertEqual(len(leaves), len(actual))

    def test_manual_and_retained_dead_terminals_keep_one_grid_cell_each(self):
        self.tmux("set-option", "-w", "-t", self.window, "remain-on-exit", "on")
        dead = self.tmux(
            "split-window",
            "-d",
            "-P",
            "-F",
            "#{pane_id}",
            "-t",
            self.window,
            "/bin/sh",
            "-c",
            "exit 7",
        )
        live_tmux.wait(lambda: TmuxCli().status(dead) == 7, what="retained terminal exit")
        manual = self.tmux(
            "split-window", "-d", "-P", "-F", "#{pane_id}", "-t", self.window, *live_tmux.IDLE
        )
        self.tui("tile", self.window)
        self.assert_complete()
        order = self.tmux("list-panes", "-t", self.window, "-F", "#{pane_id}").split()
        self.assertEqual(
            order,
            [
                self.manager,
                dead,
                self.server.pane_of("w1"),
                self.server.pane_of("w2"),
                manual,
            ],
        )
        self.assertEqual(self.tmux("display-message", "-p", "-t", dead, "#{pane_dead}"), "1")

    def test_duplicate_and_invalid_claims_do_not_drop_or_duplicate_real_panes(self):
        for name, pane in (("duplicate", self.server.pane_of("w1")), ("invalid", "%999999")):
            self.tmux("new-session", "-d", "-s", name, *live_tmux.IDLE)
            self.tmux("set-option", "-t", f"={name}:", "@opener", MANAGER)
            self.tmux("set-option", "-t", f"={name}:", "@pane", pane)
        self.tui("tile", self.window)
        self.assert_complete()
        self.grid([["w1", "w2"]])

    def test_zoomed_layout_is_unchanged(self):
        self.tmux("resize-pane", "-Z", "-t", self.manager)
        before = self.layout()
        self.tui("tile", self.window)
        self.assertEqual(self.layout(), before)
        self.assertEqual(self.display(MANAGER, "#{window_zoomed_flag}"), "1")

    def test_no_fit_preserves_the_previous_fitting_layout(self):
        self.started("w3")
        self.tmux("select-layout", "-t", self.window, "even-horizontal")
        self.tmux("resize-window", "-t", self.window, "-y", "3")
        before = self.layout()
        self.tui("tile", self.window)
        self.assertEqual(self.layout(), before)
        self.assert_complete()

    def test_removed_manager_clears_grid_and_show_rebuilds_it(self):
        self.tmux("kill-pane", "-t", self.manager)
        self.tui("tile", self.window)
        self.assertEqual(self.hooks(MANAGER), [])
        for option in (
            tmux_model.GRID_MANAGER,
            tmux_model.GRID_PER_COLUMN,
            tmux_model.GRID_PANES_SET,
        ):
            self.assertEqual(self.window_option(MANAGER, option), "")
        self.started("w3")
        manager = self.window_option(MANAGER, tmux_model.GRID_MANAGER)
        self.assertIn(manager, self.server.panes(self.window))
        self.assertEqual(self.window_option(MANAGER, tmux_model.GRID_PER_COLUMN), "3")
        self.assert_complete()


class SettleTest(Live):
    def setUp(self):
        super().setUp()
        self.retile("all")
        self.manager = self.server.inside(MANAGER)["TMUX_PANE"]
        self.window = self.display(MANAGER, "#{window_id}")
        w1 = self.tmux(
            "split-window", "-d", "-P", "-F", "#{pane_id}", "-t", self.manager, *live_tmux.IDLE
        )
        w2 = self.tmux("split-window", "-d", "-P", "-F", "#{pane_id}", "-t", w1, *live_tmux.IDLE)
        for name, opener, pane in (("w1", "old1", w1), ("w2", "old2", w2)):
            self.tmux("new-session", "-d", "-s", name, *live_tmux.IDLE)
            self.tmux(
                "set-option",
                "-t",
                f"={name}:",
                "@opener",
                opener,
                ";",
                "set-option",
                "-t",
                f"={name}:",
                "@pane",
                pane,
            )
        self.tmux("swap-pane", "-d", "-s", self.manager, "-t", w2)
        self.assertEqual(
            self.tmux("list-panes", "-t", self.window, "-F", "#{pane_id}").split(),
            [w2, w1, self.manager],
        )
        old = layout.render_layout(
            layout.parse_layout(
                f"200x50,0,0[200x25,0,0{{100x25,0,0,{w2[1:]},99x25,101,0,{w1[1:]}}},200x24,0,26,{self.manager[1:]}]"
            )
        )
        self.tmux("select-layout", "-t", self.window, old)
        self.assertEqual(layout.parse_layout(self.layout()), layout.parse_layout(old))

    def layout(self) -> str:
        return self.tmux("display-message", "-p", "-t", self.window, "#{window_layout}")

    def grid_up(self) -> None:
        path = {"PATH": os.pathsep.join((PLUGIN_BIN, os.environ.get("PATH", "")))}
        with self.inside(MANAGER), mock.patch.dict(os.environ, path):
            self.assertIsNone(
                tmux_model.Grid(TmuxCli()).configure(
                    self.window, self.manager, per_column=1, retile=tmux_model.Retile("all")
                )
            )

    def settled(self) -> str:
        self.grid([[["w1"]], [["w2"]]], 99)
        return self.steady(self.layout())

    def steady(self, layout: str) -> str:
        return self.holds(self.layout, layout, "the window's layout")

    def hand_layout(self, tiles: str) -> tuple[str, str]:
        # even-vertical: even-horizontal of these three panes already is their grid
        self.tmux("select-layout", "-t", self.window, "even-vertical")
        state = f"#{{{tmux_model.GRID_MANAGER}}}\t#{{{tmux_model.GRID_BUSY}}}\t#{{{tmux_model.GRID_TILES}}}"

        def changed():
            manager, busy, now = self.tmux("display-message", "-p", "-t", self.window, state).split(
                "\t"
            )
            return (manager, now) if not manager or now != tiles and not busy else None

        return live_tmux.wait(
            changed, BREAKER_TIMEOUT, what="the installed retile hook updates the grid"
        )

    def test_concurrent_tiles_settle(self):
        self.tmux(
            "set-option",
            "-w",
            "-t",
            self.window,
            tmux_model.GRID_MANAGER,
            self.manager,
            ";",
            "set-option",
            "-w",
            "-t",
            self.window,
            tmux_model.GRID_PER_COLUMN,
            "1",
        )
        env = self.server.env(**self.server.inside(MANAGER))
        tiles = [
            subprocess.Popen(
                [COMMAND, "tile", *flag, self.window],
                env=env,
                cwd=self.cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for flag in (["--no-wait"], [])
            for _ in range(8)
        ]
        for p in tiles:
            self.addCleanup(
                p.communicate
            )  # cleanups run last first: after the kill, reaps it, closes its pipes
            self.addCleanup(p.kill)
        for p in tiles:
            _, err = p.communicate(timeout=TIMEOUT)
            self.assertEqual(p.returncode, 0, f"{p.args}: {err}")
        self.settled()

    def test_busy_mutes_the_hooks(self):
        self.grid_up()
        grid = self.settled()
        self.tmux("set-option", "-w", "-t", self.window, tmux_model.GRID_BUSY, self.window)
        self.tmux("select-layout", "-t", self.window, "even-vertical")
        self.assertNotEqual(self.steady(self.layout()), grid)
        self.tmux("set-option", "-u", "-w", "-t", self.window, tmux_model.GRID_BUSY)
        self.tmux("select-layout", "-t", self.window, "even-vertical")
        self.settled()

    def busy(self) -> str:
        return self.window_option(MANAGER, tmux_model.GRID_BUSY)

    def test_a_tile_holding_the_lock_clears_a_stale_busy_on_each_early_return(self):
        self.grid_up()
        grid = self.settled()
        panes = self.pane_set(MANAGER)
        for case, unset, flags in (
            ("laid out, pane set recorded", (), ()),
            ("laid out, pane set unrecorded", (tmux_model.GRID_PANES_SET,), ()),
            ("pane set unchanged", (), ("--if-panes-changed",)),
        ):
            with self.subTest(case):
                for option in unset:
                    self.tmux("set-option", "-u", "-w", "-t", self.window, option)
                self.tmux("set-option", "-w", "-t", self.window, tmux_model.GRID_BUSY, self.window)
                self.tui("tile", *flags, self.window)
                self.assertEqual(self.busy(), "")
                self.assertEqual(self.window_option(MANAGER, tmux_model.GRID_PANES_SET), panes)
                self.assertEqual(self.steady(self.layout()), grid)

    def test_fiftieth_retile_keeps_grid_and_fifty_first_hook_trips_breaker(self):
        self.grid_up()
        self.settled()
        now = int(time.monotonic() * 1000)
        tiles = " ".join([str(now)] * (tmux_model.TILE_LIMIT - 1))
        self.tmux("set-option", "-w", "-t", self.window, tmux_model.GRID_TILES, tiles)
        start = time.monotonic()
        manager, tiles = self.hand_layout(tiles)
        self.assertEqual(manager, self.manager)
        self.assertEqual(len(tiles.split()), tmux_model.TILE_LIMIT)
        manager, _ = self.hand_layout(tiles)
        self.assertFalse(manager)
        self.assertLess(time.monotonic() - start, BREAKER_TIMEOUT)
        options = (
            tmux_model.GRID_MANAGER,
            tmux_model.GRID_PER_COLUMN,
            tmux_model.GRID_TILES,
            tmux_model.GRID_BUSY,
            tmux_model.GRID_PANES_SET,
        )
        self.assertEqual([self.window_option(MANAGER, k) for k in options], [""] * len(options))
        hooks = self.tmux("show-hooks", "-w", "-t", self.window)
        self.assertEqual([h for h in tmux_model.GRID_HOOKS if h in hooks], [], hooks)
        self.tmux("select-layout", "-t", self.window, "even-vertical")
        self.steady(self.layout())
        self.started("w3")
        self.grid([[["w1"], ["w2"], "w3"]])
