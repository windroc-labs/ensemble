"""A caller for headless_integration_test.py: starts the headless run its JSON argument describes
under a session harness, prints its sid, then sleeps holding the view until killed.
"""

import json
import sys
import time

from ensemble.session.engine.types import Access, HookCallback, Mode, Spec
from ensemble.session.hosts.base import Host
from tests.session import harnesses


def main(argument: str) -> None:
    args = json.loads(argument)
    session_cls = harnesses.Threaded.configured(
        clients=args["clients"], hosts=Host.parse_hosts(args["hosts"])
    )
    module, state, roots = args["handler"]
    view = session_cls.start(
        Spec(
            workdir=args["workdir"],
            access=Access(cwd=args["cwd"]),
            client="claude",
            mode=Mode.HEADLESS,
            prompt="hello",
            handlers=(HookCallback(module, state, tuple(roots)),),
        )
    )
    print(view.sid, flush=True)
    time.sleep(120)


if __name__ == "__main__":
    main(sys.argv[1])
