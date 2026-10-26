"""Run the real delegate CLI with a fake interpreter at its external session boundary."""

import os
import sys

from ensemble.delegate.cli import main

if __name__ == "__main__":
    sys.executable = os.environ["FAKE_SESSION_PYTHON"]
    sys.exit(main())
