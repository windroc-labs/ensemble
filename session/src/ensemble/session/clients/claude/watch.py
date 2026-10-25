"""The Claude reply watcher, run as `python -P -s -m <this module> <state file>` (replies.WATCH) by
a hook; never imported by its package, so runpy runs it once.
"""

import sys

from ensemble.session.clients import replies as shared
from ensemble.session.clients.claude import replies

if __name__ == "__main__":
    sys.exit(shared.main(sys.argv[1:], replies.watch))
