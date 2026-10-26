"""The credentials inventory is checked against config and broker without real secrets."""

import tomllib

from tests.case import REPO
from tests.guard_checks import credentials
from tests.guard_checks.credentials import _required_keys, _services, _violations
from tests.guard_samples import Sample

CONFIG = """
work_dir = "~/.ensemble"
other_key = ""

[roles.pm]
max_runs = 3
next = ""

[linear]
team = ""
task_label_group = ""
agent_label_group = ""

[linear.harness]
client_id = ""
secret_service = ""
token_service = ""
user_id = ""

[linear.manager]
client_id = ""
secret_service = ""
token_service = ""
user_id = ""

[linear.states]
todo = ""
done = ""

[linear.task_labels]

[linear.agent_labels]

[linear.roles.pm]
client_id = ""
secret_service = ""
token_service = ""
user_id = ""
"""

KEYS = [
    "linear.task_label_group",
    "linear.agent_label_group",
    "linear.harness.client_id",
    "linear.harness.secret_service",
    "linear.harness.token_service",
    "linear.harness.user_id",
    "linear.manager.client_id",
    "linear.manager.secret_service",
    "linear.manager.token_service",
    "linear.manager.user_id",
    "linear.states.todo",
    "linear.states.done",
    "linear.task_labels",
    "linear.agent_labels",
]

SERVICES = ["ensemble-claude", "ensemble-tokens"]

GO = """package broker

var KeychainServices = []string{
	"ensemble-claude",
	// a comment
	"ensemble-tokens", // trailing

}
"""

# Built by concatenation so no file holds a secret-like literal.
SECRETS = {
    "ghs" + "_": "ghs_",
    "ghp" + "_": "ghp_",
    "gho" + "_": "gho_",
    "ghu" + "_": "ghu_",
    "ghr" + "_": "ghr_",
    "github" + "_pat_": "github_pat_",
    "lin" + "_api_": "lin_api_",
    "sk" + "-ant-": "sk-ant-",
    "-----BEGIN RSA PRIVATE" + " KEY-----": "-----BEGIN … PRIVATE KEY-----",
    "eyJ" + "a" * 12 + ".eyJ" + "b" * 12 + ".sig": "JWT",
}


def inventory(missing=()):
    return "\n".join(f"- `{name}`" for name in [*KEYS, *SERVICES] if name not in missing) + "\n"


class CredentialsInventory(Sample):
    def config(self):
        return tomllib.loads(CONFIG)

    def test_the_real_inventory_is_complete(self):
        self.assertEqual(credentials.credentials_inventory(REPO), [])

    def test_a_complete_inventory_passes(self):
        self.assertEqual(_violations(self.config(), SERVICES, inventory()), [])

    def test_required_keys_follow_the_config(self):
        self.assertEqual(_required_keys(self.config()), KEYS)

    def test_each_missing_config_key_is_reported(self):
        for key in KEYS:
            with self.subTest(key=key):
                self.assertEqual(
                    _violations(self.config(), SERVICES, inventory(missing={key})),
                    [f"docs/credentials.md: missing config key `{key}`"],
                )

    def test_a_key_named_without_backticks_is_missing(self):
        text = inventory(missing={"linear.states.todo"}) + "linear.states.todo\n"
        self.assertEqual(
            _violations(self.config(), SERVICES, text),
            ["docs/credentials.md: missing config key `linear.states.todo`"],
        )

    def test_a_key_named_without_its_linear_prefix_is_missing(self):
        text = inventory(missing={"linear.task_label_group"}) + "`task_label_group`\n"
        self.assertEqual(
            _violations(self.config(), SERVICES, text),
            ["docs/credentials.md: missing config key `linear.task_label_group`"],
        )

    def test_a_missing_service_is_reported(self):
        self.assertEqual(
            _violations(self.config(), SERVICES, inventory(missing={"ensemble-tokens"})),
            ["docs/credentials.md: missing keychain service `ensemble-tokens`"],
        )

    def test_each_secret_pattern_is_reported_by_line_without_the_value(self):
        for value, pattern in SECRETS.items():
            with self.subTest(pattern=pattern):
                text = inventory() + f"intro\nsee {value}tail\n"
                found = _violations(self.config(), SERVICES, text)
                line = len(KEYS) + len(SERVICES) + 2
                self.assertEqual(
                    found, [f"docs/credentials.md:{line}: secret-like value ({pattern})"]
                )
                self.assertNotIn("tail", found[0])

    def test_services_reads_only_the_declared_list(self):
        go = 'const codexService = "ensemble-codex"\n' + GO + 'var other = []string{"x"}\n'
        self.assertEqual(_services(go), (SERVICES, []))

    def test_a_non_literal_service_entry_is_reported(self):
        go = GO.replace("\t// a comment\n", "\tcodexService,\n")
        names, found = _services(go)
        self.assertEqual(names, SERVICES)
        self.assertEqual(
            found,
            [
                "src/broker/internal/broker/keychain.go: KeychainServices entry is not a string"
                " literal: codexService,"
            ],
        )

    def test_a_list_of_only_non_literals_reports_each_entry_and_no_list(self):
        go = "var KeychainServices = []string{\n\tcodexService,\n}\n"
        root = self.tree(
            {
                "docs/credentials.md": inventory(),
                "src/ensemble/orchestrator/config.toml": CONFIG,
                "src/broker/internal/broker/keychain.go": go,
            }
        )
        self.assertEqual(
            credentials.credentials_inventory(root),
            [
                "src/broker/internal/broker/keychain.go: KeychainServices entry is not a string"
                " literal: codexService,",
                "src/broker/internal/broker/keychain.go: no KeychainServices list",
            ],
        )

    def test_missing_files_and_list_are_violations(self):
        files = {
            "docs/credentials.md": inventory(),
            "src/ensemble/orchestrator/config.toml": CONFIG,
            "src/broker/internal/broker/keychain.go": GO,
        }
        self.assertEqual(credentials.credentials_inventory(self.tree(files)), [])
        for rel in files:
            with self.subTest(missing=rel):
                root = self.tree({k: v for k, v in files.items() if k != rel})
                self.assertEqual(credentials.credentials_inventory(root), [f"{rel}: missing"])
        for go in ("package broker\n", "var KeychainServices = []string{\n}\n"):
            with self.subTest(go=go):
                root = self.tree({**files, "src/broker/internal/broker/keychain.go": go})
                self.assertEqual(
                    credentials.credentials_inventory(root),
                    ["src/broker/internal/broker/keychain.go: no KeychainServices list"],
                )
        root = self.tree({**files, "src/ensemble/orchestrator/config.toml": "team ="})
        found = credentials.credentials_inventory(root)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].startswith("src/ensemble/orchestrator/config.toml: "))
