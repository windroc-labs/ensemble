"""tui_support.Stream's daemon and scoped exception hook preserve events and failures."""

from types import SimpleNamespace

from ensemble.session import Reply
from tests.session.case import Case
from tests.session.tui_support import Stream


class FakeHandle:
    def __init__(self, error=None):
        self.error = error
        self.stopped = False

    def events(self):
        yield Reply("first")
        if self.error is not None:
            raise self.error

    def stop(self):
        self.stopped = True


class StreamTest(Case):
    def stream(self, handle):
        cleanups = []
        case = SimpleNamespace(
            addCleanup=lambda call, *args: cleanups.append((call, args)),
            enterContext=self.enterContext,
        )
        stream = Stream(case, handle)
        self.assertEqual(cleanups, [(stream.close, (handle,))])
        self.assertTrue(stream.thread.daemon)
        return stream

    def test_event_and_managed_cleanup(self):
        handle = FakeHandle()
        stream = self.stream(handle)
        self.assertEqual(stream.next(), Reply("first"))
        stream.close(handle)
        self.assertTrue(handle.stopped)

    def test_exception_hook_forwards_the_original_error(self):
        error = ValueError("worker failed")
        handle = FakeHandle(error)
        stream = self.stream(handle)
        self.assertEqual(stream.next(), Reply("first"))
        with self.assertRaises(ValueError) as caught:
            stream.next()
        self.assertIs(caught.exception, error)
        stream.close(handle)
        self.assertTrue(handle.stopped)
