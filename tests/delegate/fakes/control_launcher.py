"""Run the real delegate CLI on DrivenDelegate (harness_session.py), as in its test."""

import sys

from ensemble.delegate.cli import main
from tests.delegate.fakes.harness_session import DrivenDelegate

if __name__ == "__main__":
    sys.exit(main(delegate_cls=DrivenDelegate))
