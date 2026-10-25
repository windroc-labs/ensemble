"""A usage-only Client returns a caller-owned sentinel without selecting a client mode."""

import sys

from ensemble.session import Usage

from session.tests.fake_client import FakeClient, Launch


class UsageClient(FakeClient):
    keys = frozenset({"label", "cwd"})
    usage_result: Usage | None = None
    queried: list[str] = []
    starts: list[str] = []

    def usage_probe(self):
        return Launch((sys.executable, "-c", "print('quota')"), cwd=self.config["cwd"])

    def usage(self, output):
        self.queried.append(self.config["label"])
        return self.usage_result

    def headless(self):
        self.starts.append("headless")
        raise AssertionError("usage started a headless client")

    def tui(self):
        self.starts.append("tui")
        raise AssertionError("usage started a TUI client")

    def in_process(self):
        self.starts.append("in_process")
        raise AssertionError("usage started an in_process client")
