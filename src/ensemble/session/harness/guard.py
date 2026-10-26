"""The guard (stdlib only, run by path): every Host's managed worker runs it as the client's direct
parent (WDRC-199 FR-13 to FR-15). Its argv and channel protocol: docs/sessions.md › Guard protocol.
"""

import sys


def main(argv: list[str] | None = None) -> int:
    raise NotImplementedError("the guard is not implemented yet (WDRC-199 FR-13)")


if __name__ == "__main__":
    sys.exit(main())
