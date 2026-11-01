"""A process-free Delegate boundary for orchestrator scheduling and launch scenarios: a run is
alive while api.live holds its harness name `ensemble-<role>-<ID>`; a constructed run records
(role, spec, options) in api.constructed (construct_error raises), its detach in api.detached
(detach_error raises) with runs.jsonl's text then in api.runs_at_detach, and puts its name in
api.live; its Record shows its display option; its outcome is api.outcomes' for its issue;
check_tui records its (display, events) in api.tui_checks (tui_error raises); checkouts(W) is
api.checkouts' for W, asked logged in api.asked; clean(W) appends W to api.cleaned.
"""

import os
from dataclasses import replace

from ensemble.delegate import ConfigError, Delegate, SessionError
from ensemble.orchestrator.engine import runs
from ensemble.session import Access, NoRecordedSession, SessionStatus
from ensemble.session import ConfigError as SessionConfigError
from ensemble.session import Spec as SessionSpec
from ensemble.utils import Record, Status


def harness_name(role, issue):
    return f"ensemble-{role}-{issue}"


def provider(api, cfg):
    class ScenarioDelegate(Delegate):
        def __init__(self, role, spec, **options):
            api.constructed.append((role, spec, options))
            if api.construct_error is not None:
                raise api.construct_error
            self.role, self.spec, self.issue = role, spec, os.path.basename(spec.workdir)

        @classmethod
        def create(cls, role, spec, *, display=None, callback=None, overlays=()):
            return cls(role, spec, display=display, callback=callback, overlays=overlays)

        @classmethod
        def attach(cls, record_or_workdir, *, sid=None):
            workdir = (
                record_or_workdir.workdir
                if isinstance(record_or_workdir, Record)
                else record_or_workdir
            )
            view = cls.__new__(cls)
            view.workdir = workdir
            view.issue = os.path.basename(workdir)
            entries = [e for e in runs.parse_log(cfg.runs_log) if e.issue == view.issue]
            if not entries:
                raise ConfigError(f"{view.issue}: no recorded run")
            view.entry = entries[-1]
            view.role = view.entry.role
            if sid is not None and sid != view.entry.sid:
                raise ConfigError(f"sid {sid} is not the run's")
            return view

        @classmethod
        def roles(cls):
            return {role: {task: task for task in data.tasks} for role, data in cfg.roles.items()}

        @classmethod
        def resolve(cls, role, task=None, *, overlays=()):
            run = replace(cfg.roles[role].run, task=task or "")
            for overlay in overlays:
                selected = (
                    overlay.get("roles", {})
                    .get(role, {})
                    .get("client", overlay.get("client", run.client))
                )
                run = replace(run, client=selected)
            return run

        @classmethod
        def trusted_dirs(cls):
            return frozenset()

        @classmethod
        def check_tui(cls, *, display=None, events=None):
            api.tui_checks.append((display, events))
            if api.tui_error is not None:
                raise api.tui_error

        @classmethod
        def checkouts(cls, workdir):
            api.asked.append((cls, workdir))
            return api.checkouts.get(workdir)

        @classmethod
        def clean(cls, workdir, *, dry=False, scratch=False):
            api.cleaned.append((workdir, scratch) if scratch else workdir)
            return True

        def alive(self):
            if api.live is None:
                raise SessionError("agent runs unknown")
            return harness_name(self.role, self.issue) in api.live

        def content(self):
            return self.status().to_line()

        def status(self):
            live = self.alive()
            return Status(
                name=self.issue,
                alive=live,
                state="working" if live else "gone",
                outcome=None,
                attach=self.attach_command(),
                hidden=False,
            )

        def outcome(self):
            return api.outcomes.get(self.issue)

        def show(self):
            return api.show(harness_name(self.role, self.issue))

        def hide(self):
            api.hide(harness_name(self.role, self.issue))

        def close(self):
            return api.close(harness_name(self.role, self.issue))

        def attach_command(self):
            return api.attach_command(harness_name(self.role, self.issue))

        def session_status(self):
            client = runs.client_of(runs.parse_log(cfg.runs_log), self.entry.sid)
            try:
                return api.inspect(
                    SessionSpec(
                        workdir=self.workdir,
                        access=Access(cwd=self.workdir),
                        client=client,
                        sid=self.entry.sid,
                    )
                )
            except NoRecordedSession:
                return SessionStatus("no", None, "")
            except SessionConfigError as error:
                raise ConfigError(str(error)) from None

        def detach(self, *, outer=None):
            issue = os.path.basename(self.spec.workdir)
            name = harness_name(self.role, issue)
            api.order.append(("detach", name))
            api.detached.append((self.role, self.spec, api.constructed[-1][2]))
            with open(cfg.runs_log, encoding="utf-8") as f:
                api.runs_at_detach.append(f.read())
            if api.detach_error is not None:
                raise api.detach_error
            if api.live is not None:
                api.live.append(name)
            return Record(
                1,
                (outer or {}).get("kind", "role"),
                issue,
                (name,),
                self.spec.sid,
                self.spec.workdir,
                tuple((outer or {}).get("ctl", ("ensemble-delegate",))),
                tuple((outer or {}).get("resume", ("ensemble-delegate", "resume", issue))),
                api.constructed[-1][2].get("display"),
                None,
            )

        def run(self, on_event=None):
            raise AssertionError("scenario must not launch")

        def resume(self, *, mode=None, input=None):
            raise AssertionError("legacy launcher owns this scenario")

    return ScenarioDelegate
