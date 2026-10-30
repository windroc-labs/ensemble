"""Query the configured role's usage in a fresh interpreter without starting a session."""

import io
import json
import unittest
from unittest import mock

from ensemble.delegate import HarnessDelegate
from ensemble.session import Usage, Window
from tests.delegate.fakes.harness_session import HarnessSession, RecordingHost
from tests.delegate.fakes.usage import UsageClient


def main() -> None:
    case = unittest.TestCase()
    sentinel = Usage(Window(12.5, 1767225600.0), Window(37.0, None))
    probes = []
    for expected in (sentinel, None):
        UsageClient.usage_result = expected
        process = mock.Mock(stdout=io.StringIO("quota"), stderr=io.StringIO(""), returncode=0)
        process.communicate.return_value = ("quota", "")
        with mock.patch("subprocess.Popen", return_value=process) as probe:
            case.assertIs(HarnessDelegate.usage("dummy-tester"), expected)
        case.assertEqual(probe.call_count, 1)
        probes.append(probe.call_count)
    case.assertEqual(UsageClient.queried, ["role_usage", "role_usage"])
    case.assertEqual(
        (UsageClient.starts, HarnessSession.started, RecordingHost.launches), ([], [], [])
    )
    print(
        json.dumps(
            {
                "queried": UsageClient.queried,
                "probes": probes,
                "starts": UsageClient.starts,
                "session_starts": HarnessSession.started,
                "launches": RecordingHost.launches,
            }
        )
    )


if __name__ == "__main__":
    main()
