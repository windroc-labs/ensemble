"""The remote Docker target guide: both platforms' preparation, the runner isolation basis, and its links."""

from pathlib import Path

from tests.case import REPO, Case

GUIDE = Path(REPO, "docs", "remote-docker-target.md")


def section(text, heading):
    """The body under a `## heading`, up to the next `## `."""
    return text.split(f"\n## {heading}\n", 1)[1].split("\n## ", 1)[0]


class TargetGuideTest(Case):
    def test_target_guide_covers_both_platforms(self):
        text = GUIDE.read_text()
        for heading, extra in (
            ("Linux target", ("sshd_config.d/00-", "sshd -T", "ss -ltnp")),
            ("Windows target", ("Match", "DefaultShell", 'sshd.exe" -T', "Get-NetTCPConnection")),
        ):
            with self.subTest(heading=heading):
                body = section(text, heading)
                for want in (
                    "26",
                    "known_hosts",
                    "PasswordAuthentication no",
                    "KbdInteractiveAuthentication no",
                    "PreferredAuthentications=password",
                    *extra,
                ):
                    self.assertIn(want, body)
        self.assertIn("Tailscale", section(text, "Outside the LAN"))
        self.assertIn("install-target-egress.sh", section(text, "Egress proxy on the target"))

    def test_windows_guide_changes_no_runner_distro_or_vm(self):
        body = section(GUIDE.read_text(), "Windows target")
        for want in (
            "OpenSSH Server",
            "Docker Desktop",
            "WSL distro",
            "CI runner",
            "not yet verified",
        ):
            self.assertIn(want, body)

    def test_runner_isolation_basis_and_socket_consequence(self):
        body = section(GUIDE.read_text(), "Sharing Docker Desktop with the CI runner")
        for want in (
            "no Docker socket",
            "no capabilities",
            "read-only",
            "docker system prune --volumes",
            "not checked on the PC",
            "github-CI-runners",
        ):
            self.assertIn(want, body)

    def test_readme_and_upgrading_point_to_the_guide(self):
        readme = Path(REPO, "README.md").read_text()
        self.assertIn("(docs/remote-docker-target.md)", section(readme, "Docs"))
        upgrading = Path(REPO, "docs", "upgrading.md").read_text()
        for heading in ("Egress proxy", "Live checks"):
            with self.subTest(heading=heading):
                self.assertIn("install-target-egress.sh", section(upgrading, heading))
        self.assertIn("~/.egress/current/start.sh", upgrading)
