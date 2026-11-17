from tests.case import REPO
from tests.guard_checks.ownership import docker_target_owner
from tests.guard_samples import Sample


class DockerTargetOwner(Sample):
    def found(self, files):
        return docker_target_owner(self.tree(files))

    def test_each_second_owner_is_flagged_with_its_file(self):
        for rel, text, why in (
            (
                "src/ensemble/session/engine/target.py",
                'PATH = ".ensemble/docker/target.json"\n',
                "target.json outside src/broker/internal/docker/",
            ),
            (
                "src/broker/internal/launcher/target.go",
                'const f = ".ensemble/docker/target.json"\n',
                "target.json outside src/broker/internal/docker/",
            ),
            (
                "src/ensemble/session/engine/daemon.py",
                'TARGET = re.compile(r"ssh://([A-Za-z0-9._-]{1,64}@)?[A-Za-z0-9.-]+")\n',
                "ssh:// address in Python",
            ),
            (
                "src/ensemble/session/hosts/docker/host.py",
                'gone = "no such volume" in stderr.lower()\n',
                "Docker stderr text match in Python",
            ),
            (
                "src/ensemble/session/hosts/docker/host.py",
                'down = "Cannot connect to the Docker daemon" in stderr\n',
                "Docker stderr text match in Python",
            ),
            (
                "src/ensemble/session/clients/claude/common.py",
                'HOME = "~/.ensemble/docker-state"\n',
                "docker-state path in client code",
            ),
        ):
            with self.subTest(rel=rel, why=why):
                self.assertEqual(self.found({rel: text}), [f"{rel}: {why}"])

    def test_the_owners_and_docs_may_name_them(self):
        self.assertEqual(
            self.found(
                {
                    "src/broker/internal/docker/target.go": 'const f = ".ensemble/docker/target.json"\n',
                    "src/broker/internal/docker/docker.go": '// "ssh://u@pc"; "no such volume"\n',
                    "src/ensemble/session/README.md": "~/.ensemble/docker/target.json\n",
                    "src/broker/internal/launcher/egress.go": 'const hint = "ssh://"\n',
                    "src/ensemble/session/hosts/docker/mounts.py": 'STATE = "docker-state"\n',
                    "docs/upgrading.md": "target.json\n",
                }
            ),
            [],
        )

    def test_the_repository_passes(self):
        self.assertEqual(docker_target_owner(REPO), [])
