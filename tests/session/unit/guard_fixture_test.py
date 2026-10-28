"""The guard fixtures publish each file a test polls for whole: it exists only once complete."""

import os
import tempfile
from unittest import mock

from tests.session import docker_attach_fixture, guard_fixture
from tests.session.case import Case

FIXTURES = (guard_fixture, docker_attach_fixture)


def published(fixture, path: str, text: str) -> list[tuple[bool, str]]:
    """fixture._publish(path, text); at each os.replace, whether path existed and the source."""
    seen, replace = [], os.replace

    def recorded(source, target):
        with open(source) as stream:
            seen.append((os.path.exists(target), stream.read()))
        replace(source, target)

    with mock.patch("os.replace", side_effect=recorded):
        fixture._publish(path, text)
    return seen


class PublishTest(Case):
    def test_a_published_file_appears_complete_and_leaves_no_temp_file(self):
        for fixture in FIXTURES:
            with self.subTest(fixture.__name__), tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, "client.pid")
                self.assertEqual(published(fixture, path, "4242"), [(False, "4242")])
                with open(path) as stream:
                    self.assertEqual(stream.read(), "4242")
                self.assertEqual(os.listdir(directory), ["client.pid"])

    def test_a_republished_file_is_replaced_whole(self):
        for fixture in FIXTURES:
            with self.subTest(fixture.__name__), tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, "harness.json")
                fixture._publish(path, '{"guard": 1, "generation": "long"}')
                self.assertEqual(published(fixture, path, "{}"), [(True, "{}")])
                with open(path) as stream:
                    self.assertEqual(stream.read(), "{}")
                self.assertEqual(os.listdir(directory), ["harness.json"])
