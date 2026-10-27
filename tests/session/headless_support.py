import sys
import tempfile

from ensemble.session.clients.base import Client
from ensemble.session.engine.types import Access, Launch, Prepared, Reply, Spec, Stderr
from ensemble.session.modes.client_adapters.headless import JsonHeadless
from ensemble.session.modes.headless import HeadlessMode
from tests.session.case import Case
from tests.session.fakes.terminal import MemoryHost

SID = "s1"
NOTHING = (sys.executable, "-I", "-c", "")


class StubClient(Client):
    forbidden_selection = ("--resume",)
    client_note = "STUB NOTE"

    def __init__(self, launched=None, run=None):
        super().__init__({})
        self.launched, self.run = launched or Launch(NOTHING), run or StubRun

    def headless(self):
        return self.run(self)

    def tui(self):
        return None


class StubRun(JsonHeadless):
    specs: list[Spec] = []

    def __init__(self, client: StubClient):
        self.client = client

    def launch(self, spec: Spec):
        self.specs.append(spec)
        return self.client.launched

    def parse(self, record):
        if "raise" in record:
            raise RuntimeError(record["raise"])
        if "diag" in record:
            yield Stderr(record["diag"])
        else:
            yield Reply(record["text"])


class HeadlessCase(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.mode, self.recorded = None, []
        StubRun.specs = []

    def host(self) -> MemoryHost:
        return MemoryHost()

    def start(self, client=None, display=None, **fields) -> Prepared:
        spec = Spec(
            **{
                "workdir": self.dir,
                "access": Access(cwd=self.dir),
                "sid": SID,
                "client": "stub",
                **fields,
            }
        )
        self.mode = HeadlessMode.start(
            spec,
            client or StubClient(),
            registry=self.session_cls._config().registry,
            record=self.recorded.append,
            host=self.host(),
            display=display,
        )
        return self.mode.prepared
