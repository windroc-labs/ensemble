"""HostContract, FR-47's Host contract on what a Host keeps once every worker start is
harness-owned (its managed workers: managed_integration_test): a subclass also inherits Case and
defines make_host() -> Host. The cannot-display case runs an in_process session on the Host.
"""

import json
import os
import uuid

from ensemble.session import Access, Host, Mode, NotShowable, Spec, Viewer
from ensemble.session.viewer import display as displays
from tests.session import fake_client, harnesses
from tests.session.modules import client_loader

OTHER_DISPLAY = "elsewhere:1.2"


class HostContract:
    def make_host(self) -> Host:
        raise NotImplementedError

    def setUp(self):
        super().setUp()
        self.host = self.make_host()

    def file(self, label: str) -> str:
        return os.path.join(self.home, f"{label}-{uuid.uuid4().hex[:8]}")

    def test_release_of_an_unknown_name_is_no_error_and_lists_nothing(self):
        name = f"nope-{uuid.uuid4().hex[:12]}"
        self.host._release(name, uuid.uuid4().hex)
        self.assertNotIn(name, self.host.list())

    def test_an_unattachable_host_names_no_attach_target(self):
        if self.host.attachable:
            self.skipTest("applies only to a Host that cannot show")
        self.assertIsNone(self.host.attach_target(f"hc-{uuid.uuid4().hex[:12]}"))

    def test_a_host_that_cannot_show_returns_each_display_as_given(self):
        if self.host.attachable:
            self.skipTest("applies only to a Host that cannot show")
        for value in (None, "", OTHER_DISPLAY):
            with self.subTest(value=value):
                self.assertEqual(displays.display(Viewer(), value, self.host), value)

    def test_a_session_on_a_host_that_cannot_show_refuses_show_attach_command_and_screen(self):
        if self.host.attachable:
            self.skipTest("applies only to a Host that cannot show")
        scenario, kind = self.file("scenario"), type(self.host)
        with open(scenario, "w", encoding="utf-8") as f:
            json.dump([{"out": {"type": "message", "text": "hi"}}], f)
        fake = "ensemble.session.clients.fake:InProcessFakeClient"
        configured = harnesses.Threaded.configured(
            clients={
                "fake": {"implementation": fake, "scenario": scenario, "log": self.file("log")}
            },
            hosts=Host.parse_hosts(
                {
                    self.host.name: {"implementation": f"{kind.__module__}:{kind.__qualname__}"},
                    "default": self.host.name,
                }
            ),
            class_loader=client_loader({"ensemble.session.clients.fake": fake_client}),
            host_factory=lambda *_: self.host,
        )
        work = self.file("work")
        session = configured.start(
            Spec(
                workdir=work,
                access=Access(cwd=work),
                client="fake",
                mode=Mode.IN_PROCESS,
                prompt="hi",
            )
        )
        self.addCleanup(session.stop)
        self.assertIs(configured.host(), self.host)
        self.assertFalse(session.showable)
        for call in ("show", "attach_command", "screen"):  # its harness's view: no tui, no show
            with self.subTest(call=call), self.assertRaises(NotShowable) as caught:
                getattr(session, call)()
            self.assertEqual(str(caught.exception), f"{call}: tui mode only")
