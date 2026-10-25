"""The Codex steer reply watcher, run as `python -P -s -m <this module> <state file>` (replies.WATCH)
by a steer's hook; never imported by its package, so runpy runs it once.
"""

import sys

from ensemble.session.clients import replies as shared
from ensemble.session.clients.codex.tui import replies

if __name__ == "__main__":
    sys.exit(shared.main(sys.argv[1:], replies.watch))
