"""A caller-owned fake Session class (and its Host) for orchestrator unit tests."""

from ensemble.session import Headroom, NoRecordedSession, SessionStatus

LOW = [Headroom("default", 90.0)]


def _answer(value):
    if isinstance(value, BaseException):
        raise value
    return value


class FakeApi:
    """`live` None: sessions unknown; `statuses`, `usages` (default LOW) and `shown`: a value or
    an exception to raise; `records`: recorded sessions per sid. host() is itself; `order` logs
    close, show and detach; the rest are delegate_fixture's.
    """

    def __init__(self, order=None):
        self.order = [] if order is None else order
        self.live, self.errors, self.shown, self.info = [], {}, None, []
        self.statuses, self.records, self.usages = {}, {}, {}
        self.usage_calls, self.constructed, self.detached, self.runs_at_detach = [], [], [], []
        self.construct_error = self.detach_error = self.tui_error = None
        self.tui_checks, self.cleaned, self.asked = [], [], []
        self.outcomes, self.checkouts = {}, {}

    def host(self, name=None):
        return self

    def close(self, name):
        self.order.append(("close", name))
        if name in self.errors:
            raise self.errors[name]

    def show(self, name, *, display=None, command=None):
        self.order.append(("show", name, display, command))
        return _answer(self.shown)

    def hide(self, name, *, if_closed=False):
        self.order.append(("hide", name))

    def inspect(self, spec):
        entry = self.records.get(spec.sid)
        self.info.append((spec.client, spec.sid, entry["cwd"] if entry else spec.workdir))
        if entry is None:
            raise NoRecordedSession(f"no recorded session {spec.sid} in {spec.workdir}")
        return _answer(
            self.statuses.get(spec.sid, SessionStatus("yes", None, f"resume {spec.sid}"))
        )

    def usage(self, client):
        self.usage_calls.append(client)
        return _answer(self.usages.get(client, LOW))

    def attach_command(self, name):
        return f"attach {name}"
