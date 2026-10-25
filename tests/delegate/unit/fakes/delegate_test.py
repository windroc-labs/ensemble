"""tests/delegate/fakes/delegate.py: FakeDelegate passes DelegateContract."""

import unittest

from ensemble.delegate import Spec
from tests.delegate.case import Case
from tests.delegate.contracts.delegate import DelegateContract
from tests.delegate.fakes.delegate import FakeDelegate


class FakeDelegateContract(DelegateContract, Case):
    delegate_cls = FakeDelegate
    ROLE = "dummy"
    SHOWABLE = False

    def make(self, workdir, **kw):
        role = kw.pop("role", self.ROLE)
        return FakeDelegate(role, Spec("hello", workdir, **kw))

    def end(self, record):
        FakeDelegate.end(record.workdir)

    def finish(self, record):
        FakeDelegate.finish(record.workdir)


if __name__ == "__main__":
    unittest.main()
