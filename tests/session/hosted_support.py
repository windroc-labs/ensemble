"""Shared fake-client fixtures for HostedSession tests."""

import dataclasses
import json
import os
import tempfile
import threading
import types
from unittest import mock

from ensemble.session import Host, Session
from ensemble.session.engine import types as engine_types
from ensemble.session.engine.records import RunLog
from ensemble.session.engine.tmux_cli import TmuxCli
from ensemble.session.engine.types import Access, ConfigError, Exited, Launch, Spec, TurnFailed
from ensemble.session.modes.in_process import RUN_THREAD
from tests.session import fake_client, harnesses
from tests.session.case import Case
from tests.session.fakes.terminal import MemoryViewer, ScreenHost
from tests.session.modules import client_loader
from tests.session.threads import capture_errors
from tests.session.tmux_fixtures import which

WAIT = 10

SID = "0f8fad5b-d9cb-469f-a165-70867728950e"

PYTHONPATH = "/child"


BAD_NAMES = ("../x", "a b", "", "a/b", "w1\n", 5)

LAUNCHED: list[Spec] = []


class QuietClient(fake_client.FakeClient):
    client_note = ""


class CheckedClient(fake_client.InProcessFakeClient):
    def in_process(self):
        return CheckedRun(self)


class CheckedRun(fake_client.FakeInProcess):
    """Records preflight and rejects tier 4."""

    def __init__(self, client):
        super().__init__(client)
        self.failure = ConfigError("tier 4 is unmapped")

    def check(self, spec: Spec):
        self.checked = spec

        LAUNCHED.append(spec)
        if spec.tier == 4:
            raise self.failure
        with open(self.owner.config["log"], "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "check": True,
                        "sid": spec.sid,
                        "resume": spec.resume,
                        "recorded": os.path.exists(RunLog(spec.workdir).path),
                    }
                )
                + "\n"
            )

    def run(self, spec: Spec):
        assert spec == self.checked
        yield from super().run(spec)


class ProbeClient(fake_client.FakeClient):
    keys = fake_client.FakeClient.keys | {"probe"}

    def usage_probe(self):
        return Launch(**self.config["probe"])

    def usage(self, output):
        return engine_types.Usage(engine_types.Window(float(output), None), None)

    def session_status(self, info, *, env=types.MappingProxyType({})):
        return engine_types.SessionStatus("yes", 1.5, f"probe {info.sid} {info.cwd}")


class PoolClient(ProbeClient):
    """accounts: {name: identity}; each account's probe is ("probe-cmd", identity), whose output
    "<five>,<week>" it reads.
    """

    keys = ProbeClient.keys | {"accounts"}

    def _accounts(self):
        return frozenset(self.config["accounts"])

    def _usage_probes(self):
        return {
            name: Launch(("probe-cmd", identity))
            for name, identity in self.config["accounts"].items()
        }

    def usage(self, output):
        five, week = map(float, output.split(","))
        return engine_types.Usage(engine_types.Window(five, None), engine_types.Window(week, None))

    def headless(self):
        return PoolHeadless(self)


class PoolHeadless(fake_client.FakeHeadless):
    """Has a home, so a container Host's start records the account it selects."""

    def _home(self):
        return engine_types.ClientHome("pool_dir", "pool", "FAKE_HOME", "pool-token")


class EnvClient(ProbeClient):
    def headless(self):
        return EnvRun(self)

    def tui(self):
        return EnvTui(self)


class EnvRun(fake_client.FakeHeadless):
    def launch(self, spec: Spec):
        LAUNCHED.append(spec)
        launch = super().launch(spec)
        return dataclasses.replace(launch, env={**spec.access.env, **spec.env})


class EnvTui(fake_client.FakeTui):
    def launch(self, spec: Spec, hook):
        launch = super().launch(spec, hook)
        return dataclasses.replace(launch, env={**spec.access.env, **spec.env})


def package(name, cls) -> types.ModuleType:
    module = types.ModuleType(f"ensemble.session.clients.{name}")
    setattr(module, cls.__name__, cls)
    return module


PACKAGES = {
    "fake": fake_client,
    **{
        name: package(name, cls)
        for name, cls in (
            ("quiet", QuietClient),
            ("probe", ProbeClient),
            ("pool", PoolClient),
            ("checked", CheckedClient),
            ("env", EnvClient),
        )
    },
}


class Base(Case):
    """Configured fake clients."""

    def setUp(self):
        self.probe_dir = os.path.join(self.ensemble, "probe")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(self.join_runs)
        self.dir = os.path.realpath(tmp.name)
        self.workdir = os.path.join(self.dir, "work")
        self.scenario = os.path.join(self.dir, "scenario.json")
        self.log = os.path.join(self.dir, "client.log")
        self.steps()
        self.session_dependencies = {
            "class_loader": client_loader(
                {f"ensemble.session.clients.{name}": module for name, module in PACKAGES.items()}
            )
        }
        self.configure()

    def configure_session(self, clients, tmux=None) -> None:
        super().configure_session(clients, tmux)
        self.session = self.session_cls

    def join_runs(self) -> None:
        """A stopped in_process run goes on to its next yield, the fake's first logging in self.dir."""
        for thread in threading.enumerate():
            if thread.name == RUN_THREAD:
                thread.join(WAIT)
                self.assertFalse(
                    thread.is_alive(), f"an in_process run outlived its test by {WAIT} s"
                )

    def steps(self, *steps):
        with open(self.scenario, "w", encoding="utf-8") as f:
            json.dump(list(steps), f)

    def configure(self, client="fake", settings=None, tables=None, **extra):
        tables = {
            **{
                name: {
                    "implementation": f"ensemble.session.clients.{name}:{name.capitalize()}Client",
                    "scenario": self.scenario,
                    "log": self.log,
                }
                for name in PACKAGES
            },
            **(tables or {}),
        }
        tables[client].update(extra)
        self.configure_session(tables, settings or {})

    def spec(self, **fields) -> Spec:
        return Spec(**{"workdir": self.workdir, "access": Access(cwd=self.workdir), **fields})

    def start(
        self,
        client="fake",
        mode="headless",
        prompt="hello",
        spec=None,
        *,
        idle_timeout=None,
        display=None,
        host=None,
        **fields,
    ) -> Session:
        spec = dataclasses.replace(
            spec or self.spec(), client=client, mode=mode, prompt=prompt, **fields
        )
        session = self.session.start(spec, idle_timeout=idle_timeout, display=display, host=host)
        self.addCleanup(session.stop)
        return session

    def drain(self, session) -> list:
        """session.events() to its end; fails, stopping the session, instead of hanging past WAIT."""
        found, errors = [], []

        def run():
            found.extend(session.events())

        thread = threading.Thread(target=run, daemon=True)
        with capture_errors(thread, errors.append):
            thread.start()
            thread.join(WAIT)
        if thread.is_alive():
            session.stop()
            self.fail(f"events() did not end within {WAIT}s")
        if errors:
            raise errors[0]
        return found

    def run_once(self, **kwargs) -> tuple[Session, list[str]]:
        """A started session run to its end, and the argv its client logged."""
        session = self.start(**kwargs)
        self.assertEqual(self.drain(session), [TurnFailed("incomplete_turn"), Exited(0)])
        with open(self.log, encoding="utf-8") as f:
            (entry,) = [json.loads(line) for line in f]
        return session, entry["argv"]

    def written(self) -> list[dict]:
        return RunLog(self.workdir).read()

    def refused(self, error, message, call):
        with (
            mock.patch("subprocess.Popen", side_effect=AssertionError("started")) as popen,
            self.assertRaises(error) as caught,
        ):
            call()
        self.assertEqual(str(caught.exception), message)
        popen.assert_not_called()
        self.assertFalse(os.path.exists(RunLog(self.workdir).path))
        self.assertFalse(os.path.exists(self.log))


def fake_tmux() -> mock.Mock:
    host = mock.create_autospec(TmuxCli, instance=True)
    host.runner = mock.Mock(side_effect=AssertionError("a direct runner call"))
    host.has_session.return_value = False
    return host


class TuiBase(Base):
    """Tui fixtures with injected tmux responses."""

    def setUp(self):
        super().setUp()
        self.root = os.path.join(os.path.dirname(self.probe_dir), "tui")
        self.events = os.path.join(self.dir, "events")
        self.host = fake_tmux()
        self.enterContext(which())
        self.session_dependencies = {**self.session_dependencies, "host": self.host}
        self.configure(tui=True)

    def tui(self, **kwargs) -> Session:
        return self.start(mode="tui", **kwargs)

    def nothing_hosted(self):
        self.assertEqual(self.host.mock_calls, [])
        self.assertFalse(os.path.lexists(self.root))

    def name_refused(self, call, bad=(*BAD_NAMES, None)):
        """call(name) refuses each bad name before any tmux call or path use."""
        for name in bad:
            with self.subTest(name=name), self.assertRaises(ConfigError) as caught:
                call(name)
            self.assertEqual(
                str(caught.exception),
                f"name must match [A-Za-z0-9_-]+ but not none, got {name!r:.80}",
            )
        self.nothing_hosted()


class EnvBase(TuiBase):
    """Recorded probe and isolated caller environment."""

    def setUp(self):
        super().setUp()
        self.probe = mock.Mock(return_value="7")
        self.session_dependencies = {**self.session_dependencies, "probe": self.probe}
        self.configure("env", probe={"argv": ("probe-cmd",)})
        self.enterContext(mock.patch.dict(os.environ, {"PYTHONPATH": "/parent"}))
        self.access = Access(cwd=self.workdir, commands=("git",), env={"PYTHONPATH": PYTHONPATH})


class HostedTuiBase(TuiBase):
    def setUp(self):
        super().setUp()
        self.host = ScreenHost("tmux")
        self.boundary = True
        self.session_dependencies = {
            **{n: v for n, v in self.session_dependencies.items() if n != "host"},
            "viewer_factory": MemoryViewer,
        }
        self.configure(tui=True)

    def configure_session(self, clients, tmux=None):
        if not getattr(self, "boundary", False):
            super().configure_session(clients, tmux)
            return
        hosts = Host.parse_hosts(
            {
                "default": "tmux",
                "tmux": {
                    "implementation": "tests.session.fakes.terminal:ScreenHost",
                    **(tmux or {}),
                },
            }
        )
        chosen = self.host
        self.session_cls = self.session = harnesses.Threaded.configured(
            clients=clients,
            hosts=hosts,
            host_factory=lambda _kind, _name, _settings: chosen,
            **self.session_dependencies,
        )

    def nothing_hosted(self):
        self.assertEqual(self.host.calls.mock_calls, [])
        self.assertFalse(os.path.lexists(self.root))


class HostedEnvBase(EnvBase, HostedTuiBase):
    pass
