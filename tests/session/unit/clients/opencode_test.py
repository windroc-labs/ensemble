"""OpenCode's Client: its class attributes and write dirs."""

import os

from ensemble.session.clients.opencode import OpenCodeClient
from ensemble.session.engine.types import Access, Spec
from tests.session.case import Case
from tests.session.unit.clients.opencode.support import packaged_table


class OpenCodeClientTest(Case):
    def test_selection_starts_new_and_resumes_by_session(self):
        client = OpenCodeClient(packaged_table())
        self.assertEqual(client.select_session("ses_a", resume=False), ())
        self.assertEqual(client.select_session("ses_a", resume=True), ("--session", "ses_a"))
        self.assertEqual(client.identity_env, ("OPENCODE", "OPENCODE_PID", "OPENCODE_TERMINAL"))

    def test_resume_argv_runs_its_configured_executable(self):
        table = {**packaged_table(), "executable": "/opt/bin/oc"}
        self.assertEqual(
            OpenCodeClient(table).resume_argv("ses_a"), ("/opt/bin/oc", "--session", "ses_a")
        )

    def test_writable_dirs_are_the_cwd_and_access_dirs_never_reads(self):
        cwd, extra, read = (os.path.join(self.home, n) for n in ("cwd", "extra", "read"))
        for path in (cwd, extra, read):
            os.mkdir(path)
        spec = Spec(
            workdir=self.home,
            access=Access(cwd=cwd, dirs=(extra, extra), reads=(read,)),
        )
        self.assertEqual(
            OpenCodeClient(packaged_table()).writable_dirs(spec),
            (os.path.realpath(cwd), os.path.realpath(extra)),
        )
