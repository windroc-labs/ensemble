"""The remote Docker target guide: both platforms' preparation, the egress proxy, the runner isolation basis, and its links."""

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
            (
                "Windows target",
                ("Match", "cmd.exe", 'sshd.exe" -T', "Get-NetTCPConnection", "Private", "sign in"),
            ),
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
        egress = section(text, "Egress proxy on the target")
        for want in (
            "install-target-egress.sh",
            "ghcr.io/windroc-labs/egress-proxy",
            "Public",
            "exceptions:",
        ):
            self.assertIn(want, egress)
        mac = section(text, "Linux target")
        for want in ("SSH_AUTH_SOCK", "-F none", "The server may need to be upgraded"):
            self.assertIn(want, mac)
        self.assertIn(
            "gh auth logout -h github.com", section(text, "Nothing to keep on the target")
        )

    def test_windows_guide_changes_no_runner_distro_or_vm(self):
        body = section(GUIDE.read_text(), "Windows target")
        for want in (
            "OpenSSH Server",
            "Docker Desktop",
            "WSL distro",
            "CI runner",
        ):
            self.assertIn(want, body)

    def test_runner_isolation_basis_and_socket_consequence(self):
        body = section(GUIDE.read_text(), "Sharing Docker Desktop with the CI runner")
        for want in (
            "no Docker socket",
            "CapDrop=[]",
            "read-only",
            "docker system prune --volumes",
            "docker inspect gh-runner-1",
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
