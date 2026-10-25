"""Explicit subprocess wiring for the fake Linear transport: each entry runs under it. `launch`
runs router.outer on a claimed or resumed run given by its Spec fields (--name VALUE); `tui ISSUE
SID ROLE CLIENT DISPLAY INPUT` detaches a recorded run's LinearRun in tui mode at DISPLAY; `promote`
and `router` run theirs; any other is an ensemble-orchestrator command (writeback, close, resume, ...).
"""

import argparse
import functools
import os
import sys

from ensemble.orchestrator import cli
from ensemble.orchestrator.engine import config, log, promote, router
from ensemble.orchestrator.engine.linear_run import LinearRun
from ensemble.orchestrator.engine.types import Spec
from ensemble.orchestrator.trackers.linear import client
from orchestrator_tests.integration.fake_transport import install, refuse_keychain


def launch(argv):
    parser = argparse.ArgumentParser(prog="entry.py launch")
    for name in ("uuid", "issue", "project", "role", "sid", "mode", "client"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--task")
    cfg = config.load()
    log.use_logs(cfg.logs_dir)
    return router.outer(
        Spec(**vars(parser.parse_args(argv))),
        cfg,
        gql=client.linear_gql,
        run=config.shell,
        keychain=client.has_key,
        wiring=cli.TRACKERS[cfg.tracker],
    )


def tui(argv):
    issue, sid, role, run_client, display, text = argv
    cfg = config.load()
    LinearRun(
        issue,
        text,
        cfg.issue_workdir(issue),
        cfg=cfg,
        role=role,
        sid=sid,
        client=run_client,
        mode="tui",
        display=display,
    ).detach()
    return 0


def main(argv):
    entry, *arguments = argv
    sys.addaudithook(refuse_keychain)
    with install(os.environ["FAKE_LINEAR_TRANSPORT"]):
        entries = {
            "launch": launch,
            "tui": tui,
            "promote": functools.partial(
                promote.main, trackers=cli.TRACKERS, gql=client.linear_gql
            ),
            "router": functools.partial(router.main, trackers=cli.TRACKERS),
        }
        if entry in entries:
            return entries[entry](arguments)
        return cli.main([entry, *arguments])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
