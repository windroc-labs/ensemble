"""Headless and JsonHeadless contract tests."""

from ensemble.session.engine.types import (
    ClientContext,
    Launch,
    Permission,
    Reply,
    Spec,
    Stderr,
    TurnFailed,
)
from ensemble.session.modes.client_adapters.headless import Headless, JsonHeadless
from tests.session.case import Case
from tests.session.client_adapters_support import StubHeadless


class StreamHeadless(Headless):
    """Reads its client's lines itself: no parse."""

    def launch(self, spec: Spec):
        return Launch(("stub", spec.prompt))

    def events(self, lines):
        for line in lines:
            yield Reply(line.strip())


class HeadlessTest(Case):
    def test_the_stream_is_required_and_parse_is_not(self):
        self.assertEqual(list(StreamHeadless().events(["a\n", "b"])), [Reply("a"), Reply("b")])
        for interface, method in ((Headless, "events"), (JsonHeadless, "parse")):
            launch_only = type("LaunchOnly", (interface,), {"launch": StubHeadless.launch})
            with self.subTest(missing=method), self.assertRaisesRegex(TypeError, method):
                launch_only()

    def test_a_run_given_no_context_is_on_the_loopback_in_auto_mode(self):
        self.assertEqual(StreamHeadless()._context, ClientContext("127.0.0.1", Permission.AUTO))

    def test_events_parse_objects_and_fail_malformed_or_non_object_output(self):
        lines = [
            '{"texts": ["a"]}\n',
            "not json\n",
            "[1, 2]\n",
            "\n",
            "",
            '"text"\n',
            "null\n",
            "[" * 100000 + "]" * 100000 + "\n",
            "{}\n",
            '{"texts": ["b", "c"]}',
        ]
        self.assertEqual(
            list(StubHeadless().events(lines)),
            [
                Reply("a"),
                *(
                    [
                        Stderr("invalid JSON event"),
                        TurnFailed("invalid_output", "invalid JSON event"),
                    ]
                    * 5
                ),
                Reply("b"),
                Reply("c"),
            ],
        )
