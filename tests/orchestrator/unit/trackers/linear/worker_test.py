"""Worker issue commands consume broker files without host configuration or secrets."""

import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ensemble.orchestrator import cli
from ensemble.orchestrator.engine import config
from ensemble.orchestrator.trackers.linear import worker
from tests.orchestrator.case import Case
from tests.orchestrator.unit.issue_fixtures import (
    CONFIG,
    Gql,
    comment,
    conn,
    create_gql,
    issue_node,
)

MOUNT = worker.DIRECTORY  # read at import, before any Case moves it into the test's HOME


class Worker(Case):
    def setUp(self):
        super().setUp()
        cfg_path = Path(self.home) / "board.toml"
        cfg_path.write_text(CONFIG)
        self.grant = config.credentials(config.load(path=str(cfg_path)), "engineer", "build")[
            "linear"
        ]
        self.directory = Path(self.home) / "delivered"
        self.directory.mkdir()
        self.token = self.directory / "token.json"
        self.token.write_text(
            json.dumps(
                {
                    "access_token": "WORKER-TOKEN",
                    "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                }
            )
        )
        self.board = self.directory / "board.json"
        self.board.write_text(json.dumps(self.grant["board"]))
        self.enterContext(mock.patch.object(worker, "DIRECTORY", str(self.directory)))
        Path(self.ensemble, "config.local.toml").write_text("[unconfigured")

    def test_reads_the_broker_s_linear_mount(self):
        self.assertEqual(MOUNT, "/run/ensemble/secrets/linear")

    def run_cli(self, args, gql):
        def endpoint(request, **_kw):
            self.assertEqual(request.get_header("Authorization"), "Bearer WORKER-TOKEN")
            payload = json.loads(request.data)
            data = gql(payload["query"], **payload["variables"])
            return io.BytesIO(json.dumps({"data": data}).encode())

        out, err = io.BytesIO(), io.StringIO()
        with (
            mock.patch("urllib.request.urlopen", side_effect=endpoint),
            mock.patch("sys.stdout", SimpleNamespace(buffer=out)),
            mock.patch("sys.stderr", err),
        ):
            code = cli.main(["issue", *args])
        return code, out.getvalue(), err.getvalue()

    def test_reread_gets_updated_description_and_comments_without_host_config(self):
        for body, comments in (
            ("old body", []),
            ("additional requirements 中文 $HOME `ls`", [comment(1)]),
        ):
            code, output, err = self.run_cli(
                ["read", "TASK-2"], Gql(issue_node(description=body, comments=conn(comments)))
            )
            self.assertEqual((code, err), (0, ""))
            view = json.loads(output)
            self.assertEqual(view["description"], body)
            self.assertEqual(len(view["comments"]), len(comments))
            if comments:
                self.assertEqual(view["comments"][0]["body"], comments[0]["body"])

    def test_full_output_keeps_long_body(self):
        body = "补充要求\n" * 4000
        path = self.directory / "issue.json"
        code, _, err = self.run_cli(
            ["read", "TASK-2", "--full", "--output", str(path)],
            Gql(
                issue_node(description=body, comments=conn([comment(2)], more=True, cursor="next")),
                {"comments": [conn([comment(1)])]},
            ),
        )
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(path.read_text())["description"], body)
        self.assertEqual(
            [c["body"] for c in json.loads(path.read_text())["comments"]],
            ["comment 2", "comment 1"],
        )

    def test_bad_missing_and_expired_files_fail_without_local_fallback(self):
        good = self.token.read_text()
        for value in (
            "{",
            "{}",
            json.dumps({"access_token": "WORKER-TOKEN", "expires_at": "2000-01-01T00:00:00Z"}),
        ):
            self.token.write_text(value)
            code, output, err = self.run_cli(
                ["read", "TASK-2"], mock.Mock(side_effect=AssertionError("bad token sent"))
            )
            self.assertEqual(code, 2)
            self.assertEqual(output, b"")
            self.assertNotIn("WORKER-TOKEN", err)
        self.token.write_text(good)
        self.board.unlink()
        self.assertEqual(self.run_cli(["read", "TASK-2"], mock.Mock())[0], 2)

    def test_ordinary_run_can_write_with_role_grant_and_assignment_metadata(self):
        body = self.directory / "body.md"
        body.write_text("child requirement")
        gql = create_gql()
        code, _, err = self.run_cli(
            [
                "create",
                "--title",
                "Child",
                "--body-file",
                str(body),
                "--role",
                "engineer",
                "--task",
                "build",
                "--client",
                "claude",
            ],
            gql,
        )
        self.assertEqual((code, err), (0, ""))
        mutation = next(v for q, v in gql.calls if q.startswith("mutation"))
        self.assertEqual(
            mutation["in"]["delegateId"], self.grant["board"]["role_users"]["engineer"]
        )
        self.assertEqual(
            set(mutation["in"]["labelIds"]),
            {
                self.grant["board"]["labels"]["task_labels"]["build"],
                self.grant["board"]["labels"]["agent_labels"]["claude"],
            },
        )
