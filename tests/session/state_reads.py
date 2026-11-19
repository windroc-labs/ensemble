"""Reads Docker sessions' client state through client_state: one `<session> <rel>` line on stdin
at a time, printing the file's text, `none` or `error <errno>`.
"""

import sys

from ensemble.session.engine.client_state import VolumeHome
from ensemble.session.engine.types import HomePath


def main() -> None:
    for line in sys.stdin:
        session, rel = line.split()
        try:
            found = VolumeHome(session, "app").read(HomePath("/nowhere", rel))
        except OSError as error:
            print(f"error {error.errno}", flush=True)
            continue
        print("none" if found is None else found.decode(), flush=True)


if __name__ == "__main__":
    main()
