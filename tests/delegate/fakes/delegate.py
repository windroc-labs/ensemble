"""FakeDelegate, a Delegate with one session that runs nothing: it lays its workdir out per Layout,
its state the run dir's delegate.json, so a view from attach and the Delegate that wrote it agree;
clean refuses only while it is alive. end(workdir) and finish(workdir) stand for its sessions
ending, without and with an outcome; lose(workdir) for its state lost; starts(workdir) counts
its starts.
"""

import json
import os
import shutil
import uuid
from dataclasses import asdict

from ensemble.delegate import ConfigError, Delegate, Outcome, Progress
from ensemble.delegate.composer import RunConfig
from ensemble.delegate.engine.types import Begun, Finished, Layout, Result
from ensemble.session import NotShowable, SessionStatus
from ensemble.utils import Record, Status, save

FILE = "delegate.json"


def _path(workdir: str) -> str:
    return os.path.join(Layout(workdir).run, FILE)


def _load(workdir) -> dict:
    try:
        with open(_path(workdir), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, TypeError, ValueError):
        raise ConfigError(f"{workdir!r}: no {FILE}") from None


def _save(workdir: str, state: dict) -> dict:
    save(_path(workdir), json.dumps(state))
    return state


def _update(workdir: str, **changes) -> dict:
    return _save(workdir, {**_load(workdir), **changes})


class FakeDelegate(Delegate):
    ROLES = {"dummy": {"echo": "echoes its input"}}
    outcome_result = Outcome("done", "Echoed", "echoed its input", deliverable="hello")
    usage_result = None

    @classmethod
    def create(cls, role, spec, *, display=None, callback=None, overlays=()):
        return cls(role, spec, display=display, callback=callback, overlays=overlays)

    def __init__(
        self,
        role,
        spec,
        *,
        driver=None,
        session_cls=None,
        composer=None,
        display=None,
        host=None,
        harness_host=None,
        callback=None,
        overlays=(),
    ) -> None:
        self._known(role)
        sid = spec.sid or str(uuid.uuid4())
        self.workdir, self.input = os.path.abspath(spec.workdir), spec.input
        name = spec.name or f"{role}-{sid[:8]}"
        state = {"role": role, "sid": sid, "name": name, "display": display}
        os.makedirs(Layout(self.workdir).agent, exist_ok=True)
        _save(self.workdir, {**state, "outer": {}, "alive": False, "outcome": None, "starts": 0})

    @classmethod
    def _known(cls, role: str) -> None:
        if role not in cls.ROLES:
            raise ConfigError(f"no role {role!r}")

    @classmethod
    def attach(cls, record_or_workdir, *, sid=None):
        workdir = getattr(record_or_workdir, "workdir", record_or_workdir)
        state = _load(workdir)
        if sid is not None and sid != state["sid"]:
            raise ConfigError(f"sid {sid} is not the run's")
        view = cls.__new__(cls)
        view.workdir = os.path.abspath(workdir)
        return view

    @classmethod
    def usage(cls, role, *, overlays=()):
        cls._known(role)
        return cls.usage_result

    @classmethod
    def roles(cls):
        return {role: dict(tasks) for role, tasks in cls.ROLES.items()}

    @classmethod
    def resolve(cls, role, task=None, *, overlays=()):
        cls._known(role)
        return RunConfig(
            role=role,
            task=task or "",
            tier=2,
            effort="high",
            client="fake",
            output={"type": "local"},
            idle_timeout=60,
            show="",
        )

    @classmethod
    def trusted_dirs(cls):
        return frozenset()

    @classmethod
    def check_tui(cls, *, display=None, events=None):
        return None

    @classmethod
    def checkouts(cls, workdir):
        return Layout(workdir).checkouts()

    @classmethod
    def clean(cls, workdir, *, dry=False, scratch=False):
        layout = Layout(workdir)
        agent = os.path.lexists(layout.agent)
        if agent and not layout.in_place():
            raise ConfigError(f"not a real directory: {layout.agent}, refusing to touch")
        if os.path.exists(_path(workdir)) and _load(workdir)["alive"]:
            raise ConfigError(f"{layout.workdir}: delegate is still alive")
        found = [p for p in (layout.scratch if scratch else (layout.agent,)) if os.path.isdir(p)]
        if not dry:
            for path in found:
                shutil.rmtree(path)
        return bool(found)

    @classmethod
    def end(cls, workdir: str) -> None:
        _update(workdir, alive=False, outcome=None)

    @classmethod
    def finish(cls, workdir: str) -> None:
        _update(workdir, alive=False, outcome=asdict(cls.outcome_result))

    @classmethod
    def starts(cls, workdir: str) -> int:
        return _load(workdir)["starts"]

    @classmethod
    def lose(cls, workdir: str) -> None:
        os.unlink(_path(workdir))

    def _record(self, state: dict) -> Record:
        name = state["name"]
        outer = {
            "kind": "role",
            "name": name,
            "ctl": ["ensemble-delegate"],
            "resume": ["ensemble-delegate", "resume", self.workdir],
            **state["outer"],
        }
        return Record(
            1,
            outer["kind"],
            outer["name"],
            (name,),
            state["sid"],
            self.workdir,
            tuple(outer["ctl"]),
            tuple(outer["resume"]),
            state["display"],
            state["role"],
        )

    def detach(self, *, outer=None):
        return self._record(_update(self.workdir, outer=dict(outer or {}), alive=True, starts=1))

    def run(self, on_event=None):
        emit = on_event or (lambda _event: None)
        result = Result(0, self.outcome_result)
        emit(Begun(_load(self.workdir)["sid"]))
        emit(Progress("start", self.input))
        _update(self.workdir, outcome=asdict(result.outcome))
        emit(Finished(result))
        return result

    def alive(self):
        return _load(self.workdir)["alive"]

    def content(self):
        return self.status().to_line()

    def status(self):
        state = _load(self.workdir)
        outcome = state["outcome"] and state["outcome"]["status"]
        word = "working" if state["alive"] else "gone"
        return Status(
            name=state["name"],
            alive=state["alive"],
            state=word,
            outcome=outcome,
            attach=None,
            hidden=False,
            cwd=Layout(self.workdir).agent,
        )

    def outcome(self):
        found = _load(self.workdir)["outcome"]
        return None if found is None else Outcome(**found)

    def show(self):
        raise NotShowable("host fake cannot show")

    def hide(self):
        raise NotShowable("host fake cannot show")

    def attach_command(self):
        return None

    def close(self):
        _update(self.workdir, alive=False)

    def session_status(self):
        return SessionStatus("yes", None, "")

    def resume(self, *, mode=None, input=None):
        state = _load(self.workdir)
        if input is not None:
            if (
                state["alive"]
                or not state["outcome"]
                or state["outcome"]["status"] != "needs_input"
            ):
                raise ConfigError("--input requires a stopped needs_input run")
            state = _update(
                self.workdir, alive=True, outcome=None, input=input, starts=state["starts"] + 1
            )
        elif not (state["alive"] or state["outcome"]):
            state = _update(self.workdir, alive=True, starts=state["starts"] + 1)
        return self._record(state)
