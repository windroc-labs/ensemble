"""Session's class methods on Session itself: HostedSession's once Session.configure ran."""

import json
import subprocess
import unittest

from ensemble.session import SessionError
from ensemble.session.modes import tui
from tests.session import live_tmux
from tests.session.case import PY, Case

UNCONFIGURED = "session not configured: call Session.configure first"
CALLED = ("start", "attach", "list", "usage", "layout", "writable_dirs", "display", "host")
# A child: each class method on Session unconfigured, then after Session.configure; it prints
# {"unconfigured": {name: message}, "configured": {name: result or [error type, message]}}.
DISPATCH = """import json, os
from ensemble.session import Access, ConfigError, SessionError, Mode, Session, Spec
from ensemble.session.engine.types import Hosts
home = os.environ["HOME"]
spec = Spec(workdir=home, access=Access(cwd=home), client="fake", mode=Mode.HEADLESS, prompt="hi")
calls = {
    "start": lambda: Session.start(spec),
    "attach": lambda: Session.attach("w1"),
    "list": lambda: Session.list(display=""),
    "usage": lambda: Session.usage("fake"),
    "layout": lambda: Session.layout(display="x;"),
    "writable_dirs": lambda: Session.writable_dirs(spec),
    "display": lambda: Session.display("x;"),
    "host": lambda: type(Session.host()).__name__,
}
def outcome(call):
    try:
        return call()
    except (ConfigError, SessionError) as e:
        return [type(e).__name__, str(e)]
unconfigured = {n: outcome(c)[1] for n, c in calls.items()}
tmux = {"implementation": "ensemble.session.hosts.tmux:TmuxHost"}
Session.configure(clients={}, hosts=Hosts("tmux", {"tmux": tmux}))
configured = {n: outcome(c) for n, c in calls.items()}
print(json.dumps({"unconfigured": unconfigured, "configured": configured}))
"""


class SessionClassMethods(Case):
    def test_on_session_itself_each_is_hosted_sessions_configured_by_session_configure(self):
        """In a child process, so no test configures the shared classes in-process."""
        server = live_tmux.Server(self, self.home)
        server.start("unrelated")
        res = subprocess.run(
            [*PY, "-c", DISPATCH],
            env=server.child_env(),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        found = json.loads(res.stdout)
        self.assertEqual(found["unconfigured"], {name: UNCONFIGURED for name in CALLED})
        with self.assertRaises(SessionError) as caught:
            tui.TuiMode.snapshot("w1")
        read_state = [type(caught.exception).__name__, str(caught.exception)]
        self.assertEqual(read_state[0], "SessionError")
        no_fake = ["ConfigError", "no [clients.fake] table in the configured clients"]
        bad_display = ["ConfigError", "display 'x;': want printable text not ending in ;"]
        self.assertEqual(
            found["configured"],
            {
                "host": "TmuxHost",
                "start": no_fake,
                "attach": read_state,
                "list": [],
                "usage": no_fake,
                "layout": bad_display,
                "writable_dirs": no_fake,
                "display": bad_display,
            },
        )


if __name__ == "__main__":
    unittest.main()
