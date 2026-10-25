"""Stub mode objects the Client and mode contract tests share."""

from ensemble.session.engine.types import HookKind, Launch, Reply, Spec
from ensemble.session.modes.client_adapters.headless import JsonHeadless
from ensemble.session.modes.client_adapters.in_process import InProcess
from ensemble.session.modes.client_adapters.tui import Tui


class StubHeadless(JsonHeadless):
    def launch(self, spec: Spec):
        return Launch(("stub", spec.prompt))

    def parse(self, record):
        return [Reply(text) for text in record.get("texts", [])]


class StubTui(Tui):
    def launch(self, spec: Spec, hook):
        return Launch(("stub", *hook))

    def reply(self, payload):
        return payload.get("last", "")


class LabeledTui(StubTui):
    hooks = {"end": HookKind.TURN_END, "wait": HookKind.BLOCKED, "work": HookKind.ACTIVITY}


class DetailedTui(LabeledTui):
    def pending(self, payload):
        return len(payload.get("tasks", []))

    def activity(self, payload):
        return payload.get("doing", "")

    def blocked_reason(self, payload):
        return payload.get("why", "")


class StubInProcess(InProcess):
    def run(self, spec: Spec):
        yield Reply(spec.prompt)
