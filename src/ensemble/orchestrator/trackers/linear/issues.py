"""The issue command's Backend over the issue_* modules, gql bound to the manager's app."""

import contextlib
import subprocess

from ensemble.orchestrator.engine.types import IssueError, KeychainError, LinearSettings
from ensemble.orchestrator.trackers.linear import (
    issue_archive,
    issue_create,
    issue_edit,
    issue_label,
    issue_list,
    issue_lookup,
    issue_page,
    issue_read,
    issue_relation,
    issue_write,
)
from ensemble.orchestrator.trackers.linear.linear_tracker import LinearTracker


class LinearIssues:
    def __init__(self, gql, linear: LinearSettings):
        self.gql, self.linear = gql, linear

    @contextlib.contextmanager
    def _keychain(self):
        try:
            yield
        except KeychainError as error:
            raise IssueError(str(error)) from None
        except (subprocess.SubprocessError, OSError) as error:
            raise IssueError(f"Linear manager app: Keychain ({type(error).__name__})") from None

    def view(self, ident, *, full):
        with self._keychain():
            return issue_read.view(self.gql, ident, full=full)

    def page(self, query, first, cursor):
        with self._keychain():
            source = issue_list.source(self.gql, query, self.linear)
            return issue_page.fetch(self.gql, source, first, cursor)

    def walk(self, query):
        with self._keychain():
            source = issue_list.source(self.gql, query, self.linear)
            return issue_page.walk(self.gql, source, first=issue_list.PAGE)

    def lookup_page(self, query, first, cursor):
        """lookup's page, as page is list's."""
        with self._keychain():
            source = issue_lookup.source(self.gql, query, self.linear.team)
            return issue_page.fetch(self.gql, source, first, cursor)

    def lookup_walk(self, query):
        with self._keychain():
            return issue_page.walk(self.gql, issue_lookup.source(self.gql, query, self.linear.team))

    def create(self, new):
        with self._keychain():
            return issue_create.create(self.gql, *issue_create.plan(self.gql, new, self.linear))

    def edit(self, change):
        with self._keychain():
            return issue_edit.edit(self.gql, self.linear, change)

    def move(self, ident, state):
        with self._keychain():
            return issue_write.move(self.gql, self.linear, ident, state)

    def comment(self, ident, body):
        with self._keychain():
            return issue_write.comment(self.gql, ident, body)

    def relation(self, op, ident, direction, other):
        with self._keychain():
            return issue_relation.relation(self.gql, op, ident, direction, other)

    def archive_plan(self, ident):
        with self._keychain():
            return issue_archive.plan(LinearTracker(self.gql), self.gql, ident)

    def archive(self, ident):
        with self._keychain():
            return issue_archive.archive(LinearTracker(self.gql), self.gql, ident)

    def label(self, op, ident, old=None, new=None):
        with self._keychain():
            return issue_label.change(self.gql, op, ident, old, new)
