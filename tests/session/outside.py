"""External Client and Host implementations using only public extension imports."""

import sys

from ensemble.session.extension import (
    Client,
    Headless,
    Host,
    InProcess,
    Launch,
    Reply,
    Spec,
    Stderr,
    TurnEnd,
)

SCRIPT = "import sys; print(sys.argv[1]); print('done')"


class OutsideClient(Client):
    def headless(self):
        return OutsideHeadless()

    def in_process(self):
        return OutsideInProcess()


class OutsideHeadless(Headless):
    def launch(self, spec: Spec) -> Launch:
        return Launch((sys.executable, "-c", SCRIPT, spec.prompt))

    def events(self, lines):
        last = ""
        for line in lines:
            text = line.rstrip("\n")
            if text == "done":
                yield TurnEnd(last)
            elif text:
                last = text
                yield Reply(text)
            else:
                yield Stderr("outside: empty line")


class OutsideInProcess(InProcess):
    def run(self, spec: Spec):
        yield Reply(spec.prompt)
        yield TurnEnd(spec.prompt)


class OutsideHost(Host):
    """A Host running no managed worker: it only lists, and has nothing to list."""

    def list(self):
        return []
