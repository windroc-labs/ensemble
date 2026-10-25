"""A claimed run's identity and role validation."""

from dataclasses import replace

from ensemble.orchestrator.engine.types import (
    GraphQLErrors,
    KeychainError,
    LinearError,
    Spec,
)
from tests.orchestrator.case import Case


class RunSpec(Case):
    def spec(self, **overrides):
        return replace(
            Spec(
                uuid="00000000-0000-4000-8000-000000000001",
                issue="TASK-26",
                project="",
                role="engineer",
                sid="00000000-0000-4000-8000-000000000002",
                mode="new",
                client="claude",
            ),
            **overrides,
        )

    def test_identity_and_role_validation(self):
        spec = self.spec()
        self.assertIsNone(spec.identity_error())
        self.assertIsNone(spec.role_error("engineer", ("build",)))
        self.assertEqual(
            self.spec(issue="bad", sid="bad").identity_error(),
            "bad issue or session id: bad bad",
        )
        self.assertEqual(spec.role_error(None, ()), "not assigned to a role")
        self.assertEqual(
            self.spec(task="unknown").role_error("engineer", ("build",)),
            "task 'unknown' is not one of engineer's tasks (build)",
        )


class Errors(Case):
    def test_graphql_errors_keep_the_text_and_the_errors(self):
        raw = [{"message": "boom", "path": ["issue"]}]
        error = GraphQLErrors(raw)
        self.assertIsInstance(error, LinearError)
        self.assertEqual(str(error), f"linear api error: {raw}")
        self.assertEqual(error.errors, raw)

    def test_a_keychain_error_is_a_linear_error(self):
        self.assertIsInstance(KeychainError("k"), LinearError)
