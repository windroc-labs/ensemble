"""Exercise the image's actual issue launcher with broker files and a fake transport."""

import json
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path


def connection(nodes):
    return {"nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None}}


def main():
    image = sys.argv[1]
    fixture = Path(__file__).resolve().parent / "worker_issue/sitecustomize.py"
    base = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
    ]

    def run(args, mounts=()):
        command = list(base)
        for source, target in mounts:
            command.extend(["--mount", f"type=bind,source={source},target={target},readonly"])
        return subprocess.run(
            [*command, image, "ensemble-orchestrator", *args],
            capture_output=True,
            text=True,
            check=False,
        )

    help_result = run(["issue", "--help"])
    assert help_result.returncode == 0, help_result.stderr
    missing = run(["issue", "read", "TASK-2"])
    assert missing.returncode == 2 and "manager" in missing.stderr, missing
    for command in ("router", "promote"):
        rejected = run([command])
        assert rejected.returncode == 2, rejected

    with tempfile.TemporaryDirectory(prefix="worker-issue-") as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        board = {
            "team": "test-team",
            "states": {},
            "human_members": [],
            "role_users": {},
            "task_label_group": "test-tasks",
            "agent_label_group": "test-agents",
            "labels": {"task_labels": {}, "agent_labels": {}},
            "project_repos": {},
        }
        (root / "board.json").write_text(json.dumps(board))
        (root / "token.json").write_text(
            json.dumps(
                {
                    "access_token": "IMAGE-TEST-TOKEN",
                    "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                }
            )
        )
        mounts = [
            (root, "/run/ensemble/secrets/linear"),
            (root, "/tmp/worker-issue"),
            (fixture, "/opt/ensemble/src/sitecustomize.py"),
        ]
        for description, comments in (
            ("original description", []),
            (
                "updated requirements 中文 $HOME `ls`",
                [
                    {
                        "id": "comment-1",
                        "body": "new manager comment",
                        "createdAt": "2026-10-28T00:00:00Z",
                        "user": {"id": "human", "name": "Manager"},
                        "parentId": None,
                        "quotedText": None,
                        "onBehalfOf": None,
                    }
                ],
            ),
        ):
            node = {
                "id": "issue-2",
                "identifier": "TASK-2",
                "title": "Image test",
                "url": "https://example.test/TASK-2",
                "description": description,
                "state": {"id": "todo", "name": "Todo", "type": "unstarted"},
                "project": None,
                "projectMilestone": None,
                "assignee": None,
                "parent": None,
                **{
                    name: connection([])
                    for name in (
                        "labels",
                        "comments",
                        "relations",
                        "inverseRelations",
                        "children",
                    )
                },
            }
            node["comments"] = connection(comments)
            (root / "response.json").write_text(json.dumps({"data": {"issue": node}}))
            for path in root.iterdir():
                path.chmod(0o644)
            result = run(["issue", "read", "TASK-2"], mounts)
            assert result.returncode == 0, result.stderr
            view = json.loads(result.stdout)
            assert view["description"] == description, view
            assert [c["body"] for c in view["comments"]] == [c["body"] for c in comments], view
    print(
        "Worker issue launcher read original and updated description/comments without host config."
    )


if __name__ == "__main__":
    main()
