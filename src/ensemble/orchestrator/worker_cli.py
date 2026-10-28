"""The worker's issue-only orchestrator command, using broker-delivered access."""

import argparse
import sys

from ensemble.orchestrator.issue import command


def main(argv=None, **streams):
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "issue":
        return command.main(args[1:], worker=True, **streams)
    parser = argparse.ArgumentParser(
        prog="ensemble-orchestrator",
        description="Access the run's tracker using broker-delivered settings and credentials.",
    )
    parser.add_subparsers(dest="command", required=True).add_parser(
        "issue", help="read and manage issues; ensemble-orchestrator issue --help"
    )
    parser.parse_args(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
