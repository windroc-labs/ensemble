"""Client boundary behavior."""

import contextlib
import dataclasses
import http.client
import io
import json
import urllib.error
import urllib.request
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest import mock

from ensemble.orchestrator.engine import types
from ensemble.orchestrator.engine.types import LinearError, LinearKey
from ensemble.orchestrator.trackers.linear import client, ops
from ensemble.utils import one_line
from orchestrator_tests.case import Case
from orchestrator_tests.process_fixtures import TimeoutExpired
from orchestrator_tests.unit.board_ids import TEAM
from orchestrator_tests.unit.keychain_fixtures import (
    HARNESS,
    NEW,
    OLD,
    Endpoint,
    Keychain,
    leaks,
    record,
    renewed,
    token_http_error,
)
from orchestrator_tests.unit.keychain_fixtures import (
    SECRET as APP_SECRET,
)
from orchestrator_tests.unit.linear_fixtures import (
    SECRET,
    VIEWER,
    errors,
    http_error,
    refused,
    respond,
)
from orchestrator_tests.unit.outage import FAILURES, failing


class LinearGql(Case):
    def gql(self, query=VIEWER, body=b'{"data": {}}', **kw):
        run = mock.Mock(return_value=SimpleNamespace(stdout="secret\n"))
        with (
            mock.patch("subprocess.run", run),
            mock.patch.object(
                urllib.request, "build_opener", side_effect=AssertionError("opener built")
            ),
            mock.patch.object(urllib.request, "urlopen", return_value=respond(body)) as urlopen,
        ):
            return client.linear_gql(query, **kw), run, urlopen

    def test_key_by_service_only_sent_by_urlopen(self):
        for service in ("svc-h", "linear-api-key-pm"):
            with self.subTest(service=service):
                out, run, urlopen = self.gql(
                    body=b'{"data": {"viewer": {"id": "v"}}}', identity=LinearKey(service)
                )
                self.assertEqual(out, {"viewer": {"id": "v"}})
                self.assertEqual(
                    [c.args[0] for c in run.call_args_list],
                    [["/usr/bin/security", "find-generic-password", "-s", service, "-w"]],
                )
                urlopen.assert_called_once()
                req = urlopen.call_args[0][0]
                self.assertEqual(
                    (req.full_url, req.get_header("Authorization")),
                    ("https://api.linear.app/graphql", "secret"),
                )
                self.assertEqual(json.loads(req.data)["variables"], {})

    def test_timeout_bounds_keychain_and_request(self):
        for kw, want in (({}, 30), ({"timeout": 5}, 5)):
            with self.subTest(timeout=want):
                _, run, urlopen = self.gql(
                    "query($i: String!) { issue(id: $i) { id } }",
                    i="TASK-1",
                    identity=LinearKey("svc"),
                    **kw,
                )
                self.assertEqual(run.call_args.kwargs["timeout"], want)
                self.assertEqual(urlopen.call_args.kwargs["timeout"], want)
                self.assertEqual(
                    json.loads(urlopen.call_args[0][0].data)["variables"], {"i": "TASK-1"}
                )

    def test_the_identity_is_required(self):
        with (
            mock.patch("subprocess.run", side_effect=AssertionError("Keychain read")),
            mock.patch.object(
                urllib.request, "urlopen", side_effect=AssertionError("request sent")
            ),
            self.assertRaises(TypeError),
        ):
            client.linear_gql(VIEWER)

    def test_a_bad_keychain_key_is_a_keychain_error(self):
        for out in ("\n", "lin api\n"):
            with (
                self.subTest(out=out),
                mock.patch("subprocess.run", return_value=SimpleNamespace(stdout=out)),
                self.assertRaises(types.KeychainError),
            ):
                client.linear_gql(VIEWER, identity=LinearKey("svc"))

    def test_a_bad_keychain_key_raises_naming_only_the_service(self):
        for out, key in (("\n", ""), ("lin api\n", "lin api"), ("lin\x1bapi\n", "lin\x1bapi")):
            with (
                self.subTest(key=key),
                mock.patch("subprocess.run", return_value=SimpleNamespace(stdout=out)),
            ):
                code = refused(self, lambda: client.linear_gql(VIEWER, identity=LinearKey("svc")))
                self.assertIn("svc", code)
                if key:
                    self.assertNotIn(key, code)


class Unavailable(Case):
    def setUp(self):
        p = mock.patch("subprocess.run", return_value=SimpleNamespace(stdout=SECRET + "\n"))
        p.start()
        self.addCleanup(p.stop)

    def gql(self, query, opened=None, read=None, body=b'{"data": {}}'):
        resp = respond(body)
        if read:
            resp.__enter__.return_value = mock.Mock(read=mock.Mock(side_effect=read))
        with mock.patch.object(urllib.request, "urlopen", side_effect=opened, return_value=resp):
            return client.linear_gql(query, identity=LinearKey("svc"))

    def unavailable(self, query, **kw):
        with self.assertRaises(types.Unavailable) as cm:
            self.gql(query, **kw)
        e = cm.exception
        for text in (str(e), one_line(e)):
            self.assertNotIn(SECRET, text)
        return e.op, e.status, e.reason, str(e)

    def test_unavailable_names_the_op_and_the_status_or_reason(self):
        cases = [
            (ops.Q_TEAM, {"opened": http_error(503)}, ("teams", 503, None, "teams: HTTP 503")),
            (
                ops.M_STATE,
                {"opened": http_error(500, b"<html>")},
                ("issueUpdate", 500, None, "issueUpdate: HTTP 500"),
            ),
            (
                ops.M_COMMENT,
                {"opened": http_error(400, errors("OTHER", "RATELIMITED"))},
                ("commentCreate", 400, "RATELIMITED", "commentCreate: RATELIMITED"),
            ),
            *(
                (VIEWER, kw, ("viewer", None, reason, f"viewer: {reason}"))
                for kw, reason in (
                    ({"opened": TimeoutError("timed out")}, "TimeoutError: timed out"),
                    (
                        {
                            "opened": urllib.error.URLError(
                                ConnectionRefusedError(61, "Connection refused")
                            )
                        },
                        "ConnectionRefusedError: [Errno 61] Connection refused",
                    ),
                    ({"opened": urllib.error.URLError("no\n host")}, "no host"),
                    (
                        {"opened": ConnectionResetError(54, "Connection reset by peer")},
                        "ConnectionResetError: [Errno 54] Connection reset by peer",
                    ),
                    (
                        {"read": http.client.IncompleteRead(b"{", 9)},
                        "IncompleteRead: IncompleteRead(1 bytes read, 9 more expected)",
                    ),
                    (
                        {"read": TimeoutError("The read operation timed out")},
                        "TimeoutError: The read operation timed out",
                    ),
                )
            ),
            *(
                (
                    ops.Q_TEAM,
                    {"body": body},
                    ("teams", None, "response not JSON", "teams: response not JSON"),
                )
                for body in (b"<html>502 Bad Gateway</html>", b"", b"\xff\xfe{")
            ),
        ]
        for query, kw, want in cases:
            with self.subTest(want=want, kw=kw):
                self.assertEqual(self.unavailable(query, **kw), want)

    def linear_error(self, error):
        with error, self.assertRaises(LinearError) as cm:
            self.gql(VIEWER, opened=error)
        self.assertNotIn(SECRET, str(cm.exception))
        return str(cm.exception)

    def test_other_4xx_and_malformed_errors_are_linear_errors_naming_op_and_status(self):
        for case, error in enumerate(
            (
                http_error(400, b"not json"),
                http_error(400, b"\xff\xfe{"),
                http_error(400),
                http_error(400, b'{"errors": 1}'),
                http_error(401, errors("AUTHENTICATION_ERROR")),
                http_error(401, errors("RATELIMITED")),
                http_error(403),
                *(
                    http_error(400, body)
                    for body in (
                        b"null",
                        b"[]",
                        b'{"errors": null}',
                        b'{"errors": [null, 1, {"extensions": 1}]}',
                    )
                ),
            )
        ):
            with self.subTest(case=case, code=error.code):
                self.assertEqual(
                    self.linear_error(error), f"linear api error: viewer: HTTP {error.code}"
                )

    def test_http_400_preserves_messages_and_codes_without_other_fields(self):
        body = json.dumps(
            {
                "errors": [
                    {
                        "message": "Query complexity limit exceeded",
                        "extensions": {"code": "GRAPHQL_VALIDATION_FAILED", "token": SECRET},
                        "request": {"Authorization": SECRET},
                    },
                    {"message": "Use a smaller page"},
                ],
                "data": {"secret": SECRET},
            }
        ).encode()
        error = http_error(400, body)
        with (
            mock.patch.object(error, "read", wraps=error.read) as read,
            self.assertRaises(LinearError) as cm,
        ):
            self.gql(VIEWER, opened=error)
        read.assert_called_once_with()
        self.assertEqual(
            str(cm.exception),
            "linear api error: viewer: HTTP 400: Query complexity limit exceeded "
            "(GRAPHQL_VALIDATION_FAILED); Use a smaller page",
        )
        self.assertNotIn(SECRET, str(cm.exception))
        self.assertTrue(error.closed)

    def test_http_400_with_only_a_code_keeps_its_classification(self):
        for code in ("RATELIMITED", "GRAPHQL_VALIDATION_FAILED"):
            error = http_error(
                400, json.dumps({"errors": [{"extensions": {"code": code}}]}).encode()
            )
            with self.subTest(code=code):
                if code == "RATELIMITED":
                    self.assertEqual(
                        self.unavailable(VIEWER, opened=error),
                        ("viewer", 400, "RATELIMITED", "viewer: RATELIMITED"),
                    )
                else:
                    self.assertEqual(
                        self.linear_error(error),
                        "linear api error: viewer: HTTP 400: unrecognized GraphQL error "
                        "(GRAPHQL_VALIDATION_FAILED)",
                    )

    def test_http_400_redacts_auth_before_truncating_and_normalizes_messages(self):
        for message, code, want in (
            (
                f"Invalid credential {SECRET}\ntry again",
                f"BAD_{SECRET}",
                "Invalid credential [redacted] try again (BAD_[redacted])",
            ),
            ("x" * 490 + SECRET, "BAD", "x" * 490 + "[redacted] (BAD)"),
            ("x" * 600, "BAD", "x" * 500 + "… (BAD)"),
        ):
            with self.subTest(length=len(message)):
                body = json.dumps({"errors": [{"message": message, "extensions": {"code": code}}]})
                self.assertEqual(
                    self.linear_error(http_error(400, body.encode())),
                    "linear api error: viewer: HTTP 400: " + want,
                )

    def test_http_400_limits_total_details(self):
        body = json.dumps({"errors": [{"message": "x" * 600, "extensions": {"code": "C" * 2000}}]})
        text = self.linear_error(http_error(400, body.encode()))
        self.assertTrue(text.startswith("linear api error: viewer: HTTP 400: " + "x" * 500))
        self.assertLessEqual(len(text), 1100)

    def test_http_400_with_an_unreadable_body_keeps_status_and_closes_response(self):
        for failure in (OSError("read failed"), http.client.IncompleteRead(b"{", 9)):
            error = http_error(400)
            with (
                mock.patch.object(error, "read", side_effect=failure),
                self.assertRaises(LinearError) as cm,
            ):
                self.gql(VIEWER, opened=error)
            self.assertEqual(str(cm.exception), "linear api error: viewer: HTTP 400")
            self.assertTrue(error.closed)

    def test_graphql_errors_in_a_200_stay_systemexit(self):
        with self.assertRaises(types.GraphQLErrors) as cm:
            self.gql(VIEWER, body=errors("RATELIMITED"))
        self.assertEqual(client.messages(cm.exception.errors), ["m (RATELIMITED)"])
        self.assertEqual(
            str(cm.exception),
            "linear api error: [{'message': 'm', 'extensions': {'code': 'RATELIMITED'}}]",
        )

    def test_malformed_response_is_unavailable(self):
        for body in (b"null", b"[]", b"1", b"{}", b'{"data": null}', b'{"data": []}'):
            with self.subTest(body=body), self.assertRaises(types.Unavailable):
                self.gql(VIEWER, body=body)

    def test_operation_is_the_first_field_of_the_top_level_selection_set(self):
        cases = {
            VIEWER: "viewer",
            ops.Q_TEAM: "teams",
            ops.Q_ISSUE_STATE: "issue",
            ops.M_STATE: "issueUpdate",
            ops.M_COMMENT: "commentCreate",
            ops.M_COMMENT_ID: "commentCreate",
            ops.Q_COMMENT: "comment",
            "{issues(first: 1) { nodes { id } } }": "issues",
            "query { }": "?",
            "query { 1x }": "?",
            "query": "?",
            "": "?",
        }
        for query, op in cases.items():
            with self.subTest(query=query):
                self.assertEqual(client.operation(query), op)


class Outage(Case):
    def test_each_failure_is_unavailable_and_recorded(self):
        want = {
            "5xx": (503, None),
            "timeout": (None, "TimeoutError: timed out"),
            "URLError": (None, "ConnectionRefusedError: [Errno 61] Connection refused"),
        }
        self.assertEqual(set(FAILURES), set(want))
        for name, error in FAILURES.items():
            with self.subTest(name):
                gql = failing(error)
                for _ in range(2):
                    with self.assertRaises(types.Unavailable) as cm:
                        gql(ops.Q_TEAM, t=TEAM)
                    self.assertEqual(
                        (cm.exception.op, cm.exception.status, cm.exception.reason),
                        ("teams", *want[name]),
                    )
                self.assertEqual(gql.failed, [(ops.Q_TEAM, {"t": TEAM})] * 2)

    def test_ops_names_the_failing_operations(self):
        gql = failing(
            FAILURES["5xx"], gql=lambda q, **v: {"q": q, **v}, ops={"teams", "issueUpdate"}
        )
        self.assertEqual(gql(ops.Q_ISSUE_STATE, i="I"), {"q": ops.Q_ISSUE_STATE, "i": "I"})
        for query in (ops.Q_TEAM, ops.M_STATE):
            with self.assertRaises(types.Unavailable):
                gql(query, i="I")
        self.assertEqual(gql.failed, [(ops.Q_TEAM, {"i": "I"}), (ops.M_STATE, {"i": "I"})])


class HasKey(Case):
    def test_key_existence_check_never_reads_the_secret(self):
        for code, want in ((0, True), (44, False)):
            with mock.patch("subprocess.run", return_value=SimpleNamespace(returncode=code)) as m:
                self.assertIs(client.has_key(LinearKey("svc")), want)
            self.assertEqual(
                m.call_args.args[0], ["/usr/bin/security", "find-generic-password", "-s", "svc"]
            )


class Messages(Case):
    def test_each_message_and_code_only(self):
        raw = [
            {"message": "boom", "extensions": {"code": "INPUT_ERROR"}, "path": ["issue"]},
            {"message": "two"},
            {"message": "x" * 600, "extensions": "odd"},
            None,
            {"extensions": {"code": "C"}},
        ]
        self.assertEqual(
            client.messages(raw),
            [
                "boom (INPUT_ERROR)",
                "two",
                "x" * 500 + "…",
                "unrecognized GraphQL error",
                "unrecognized GraphQL error (C)",
            ],
        )
        for value in ({"message": "m"}, "text", 1):
            with self.subTest(value=value):
                self.assertEqual(client.messages(value), ["unrecognized GraphQL error"])


class AppToken(Case):
    """An app's call: Bearer, renewal and its failures."""

    def call(self, keychain, endpoint=None, *, query=VIEWER, app=HARNESS, **kw):
        endpoint = endpoint or Endpoint(data={"viewer": {"id": "v"}})
        with (
            mock.patch("subprocess.run", keychain),
            mock.patch.object(urllib.request, "urlopen", side_effect=endpoint),
        ):
            return client.linear_gql(query, identity=app, **kw), endpoint

    def reported(self):
        found = []
        client.use(report=lambda identity, reason: found.append((identity, reason)))
        return found

    def written(self, keychain):
        return json.loads(keychain.value(HARNESS.token_service))

    def test_a_token_with_days_to_spare_is_sent_as_bearer_without_a_request(self):
        keychain = Keychain({HARNESS.token_service: (held := record(days=6))})
        out, endpoint = self.call(keychain)
        self.assertEqual(out, {"viewer": {"id": "v"}})
        self.assertEqual(endpoint.tokens, [])
        self.assertEqual(endpoint.graphql[0].get_header("Authorization"), f"Bearer {OLD}")
        self.assertEqual(keychain.value(HARNESS.token_service), held)

    def test_under_five_days_renews_writes_and_sends_the_new_token(self):
        keychain = Keychain(
            {HARNESS.token_service: record(days=4), HARNESS.secret_service: APP_SECRET}
        )
        since = datetime.now(UTC)
        _, endpoint = self.call(keychain)
        self.assertEqual(
            endpoint.tokens,
            [
                {
                    "grant_type": "client_credentials",
                    "client_id": "client-harness",
                    "client_secret": APP_SECRET,
                    "scope": "read,write",
                }
            ],
        )
        renewed(self, keychain.value(HARNESS.token_service), since)
        self.assertEqual(endpoint.graphql[0].get_header("Authorization"), f"Bearer {NEW}")
        self.assertEqual(leaks(" ".join(argv) for argv, _ in keychain.calls), [])

    def test_missing_bare_undated_and_expired_tokens_renew(self):
        undated = json.dumps({"access_token": OLD, "expires_at": "soon"})
        for value in (None, OLD, undated, record(days=-1), record(token="bad token", days=20)):
            with self.subTest(value=value):
                entries = {HARNESS.secret_service: APP_SECRET}
                entries |= {} if value is None else {HARNESS.token_service: value}
                keychain = Keychain(entries)
                _, endpoint = self.call(keychain)
                self.assertEqual(len(endpoint.tokens), 1)
                self.assertEqual(self.written(keychain)["access_token"], NEW)

    def test_each_token_is_read_from_the_keychain_per_call(self):
        keychain = Keychain({HARNESS.token_service: record(days=20)})
        self.call(keychain)
        keychain.items[HARNESS.token_service] = ("a", record(token=NEW, days=20))
        _, endpoint = self.call(keychain)
        self.assertEqual(endpoint.graphql[0].get_header("Authorization"), f"Bearer {NEW}")

    def test_the_write_keeps_a_plain_account_and_uses_stdin(self):
        for account, want in (("me@example.com", "me@example.com"), ("x -s other", None)):
            with self.subTest(account=account):
                keychain = Keychain({HARNESS.secret_service: APP_SECRET})
                keychain.items[HARNESS.token_service] = (account, record(days=1))
                self.call(keychain)
                writes = [(argv, stdin) for argv, stdin in keychain.calls if argv[1:] == ["-i"]]
                self.assertEqual(len(writes), 1)
                self.assertIn(f" -a {want or HARNESS.token_service} -X ", writes[0][1])
                self.assertEqual(
                    keychain.items[HARNESS.token_service][1], keychain.value(HARNESS.token_service)
                )
                self.assertNotIn("other", keychain.items)

    def test_scopes_are_fixed_per_kind(self):
        want = {
            types.AppKind.HARNESS: "read,write",
            types.AppKind.ROLE: "read,write,app:assignable,app:mentionable",
            types.AppKind.MANAGER: "read,write,app:assignable",
        }
        for kind, scope in want.items():
            with self.subTest(kind=kind):
                app = dataclasses.replace(HARNESS, kind=kind)
                self.assertEqual(app.scopes, scope)
                _, endpoint = self.call(Keychain({HARNESS.secret_service: APP_SECRET}), app=app)
                self.assertEqual(endpoint.tokens[0]["scope"], scope)

    def test_the_form_body_is_encoded(self):
        secret = "a&scope=read,write,admin+b"
        _, endpoint = self.call(Keychain({HARNESS.secret_service: secret}))
        self.assertEqual(endpoint.tokens[0]["client_secret"], secret)
        self.assertEqual(endpoint.tokens[0]["scope"], "read,write")

    @staticmethod
    def failures(body=None):
        """{name: (token endpoint answer, report reason)}, each answer fresh."""
        body = body or json.dumps(
            {"error": "invalid_client", "error_description": f"bad {APP_SECRET}"}
        )
        return {
            "401": (
                token_http_error(401, body.encode()),
                "HTTP 401: invalid_client: bad [redacted]",
            ),
            "network": (
                urllib.error.URLError(ConnectionRefusedError(61, "refused")),
                "ConnectionRefusedError: [Errno 61] refused",
            ),
            "not json": (b"<html>", "response not JSON"),
            "no token": ({"expires_in": 60}, "no access_token"),
            "unclean token": ({"access_token": "tok\n", "expires_in": 60}, "bad access_token"),
            "bad expiry": ({"access_token": NEW, "expires_in": True}, "bad expires_in"),
            "huge expiry": ({"access_token": NEW, "expires_in": 10**12}, "bad expires_in"),
            "huger expiry": ({"access_token": NEW, "expires_in": 10**20}, "bad expires_in"),
        }

    def test_a_failed_renewal_of_a_usable_token_reports_and_sends_the_old_one(self):
        for name, (answer, reason) in self.failures().items():
            with self.subTest(name):
                found = self.reported()
                keychain = Keychain(
                    {
                        HARNESS.token_service: (held := record(days=3)),
                        HARNESS.secret_service: APP_SECRET,
                    }
                )
                _, endpoint = self.call(keychain, Endpoint(answer, data={}))
                self.assertEqual(found, [("harness", reason)])
                self.assertEqual(keychain.value(HARNESS.token_service), held)
                self.assertEqual(endpoint.graphql[0].get_header("Authorization"), f"Bearer {OLD}")
                _, again = self.call(keychain, Endpoint(answer, data={}))
                self.assertEqual((again.tokens, found), ([], [("harness", reason)]))

    def test_a_write_that_does_not_persist_is_a_failed_renewal(self):
        found = self.reported()
        keychain = Keychain(
            {HARNESS.token_service: record(days=3), HARNESS.secret_service: APP_SECRET},
            fail_write=True,
        )
        _, endpoint = self.call(keychain)
        self.assertEqual(found, [("harness", "Keychain write of linear-app-harness-token failed")])
        self.assertEqual(endpoint.graphql[0].get_header("Authorization"), f"Bearer {OLD}")

    def test_a_failed_account_lookup_is_a_failed_renewal(self):
        found = self.reported()
        keychain = Keychain(
            {HARNESS.token_service: record(days=3), HARNESS.secret_service: APP_SECRET},
            raises=TimeoutExpired(["security"], 30),
        )
        _, endpoint = self.call(keychain)
        self.assertEqual(found, [("harness", "Keychain write of linear-app-harness-token failed")])
        self.assertEqual(endpoint.graphql[0].get_header("Authorization"), f"Bearer {OLD}")

    def test_a_concurrent_renewal_written_over_ours_is_used(self):
        other = record(token="tok-OTHER", days=30)
        keychain = Keychain(
            {HARNESS.token_service: record(days=-1), HARNESS.secret_service: APP_SECRET},
            race=other,
        )
        _, endpoint = self.call(keychain)
        self.assertEqual(keychain.value(HARNESS.token_service), other)
        self.assertEqual(endpoint.graphql[0].get_header("Authorization"), "Bearer tok-OTHER")

    def test_an_undated_token_whose_renewal_fails_is_still_used(self):
        found = self.reported()
        _, endpoint = self.call(
            Keychain({HARNESS.token_service: OLD, HARNESS.secret_service: APP_SECRET}),
            Endpoint(token_http_error(401), data={}),
        )
        self.assertEqual(found, [("harness", "HTTP 401")])
        self.assertEqual(endpoint.graphql[0].get_header("Authorization"), f"Bearer {OLD}")

    def test_an_expired_or_missing_token_whose_renewal_fails_is_a_keychain_error(self):
        fix = (
            "; fix: check linear.harness.client_id and the client secret in Keychain service "
            "linear-app-harness-secret (rotated: store the new secret), then rerun"
        )
        for value, state in ((record(days=-1), "expired"), (None, "has no token")):
            with self.subTest(state=state):
                found = self.reported()
                entries = {HARNESS.secret_service: APP_SECRET}
                entries |= {} if value is None else {HARNESS.token_service: value}
                with self.assertRaises(types.KeychainError) as cm:
                    self.call(Keychain(entries), Endpoint(token_http_error(401)))
                verb = "token expired" if state == "expired" else "has no token"
                self.assertEqual(
                    str(cm.exception),
                    f"Linear harness app {verb} and renewal failed: HTTP 401{fix}",
                )
                self.assertEqual((cm.exception.identity, found), ("harness", []))

    def test_no_client_secret_is_a_failed_renewal_naming_the_fix(self):
        found = self.reported()
        self.call(Keychain({HARNESS.token_service: record(days=1)}))
        self.assertEqual(
            found,
            [
                (
                    "harness",
                    "no client secret in Keychain service linear-app-harness-secret; fix: "
                    "security add-generic-password -s linear-app-harness-secret -a "
                    "linear-app-harness-secret -w",
                )
            ],
        )

    def test_without_renewal_a_due_token_is_used_and_an_expired_one_refused(self):
        client.use(renew=False)
        keychain = Keychain(
            {HARNESS.token_service: record(days=1), HARNESS.secret_service: APP_SECRET}
        )
        _, endpoint = self.call(keychain)
        self.assertEqual(endpoint.tokens, [])
        self.assertEqual(endpoint.graphql[0].get_header("Authorization"), f"Bearer {OLD}")
        keychain.items[HARNESS.token_service] = ("a", record(days=-1))
        with self.assertRaises(types.KeychainError) as cm:
            self.call(keychain)
        self.assertIn("renewal failed: dry run: no renewal", str(cm.exception))
        self.assertFalse([argv for argv, _ in keychain.calls if argv[1:] == ["-i"]])

    def test_the_default_report_is_stderr(self):
        keychain = Keychain(
            {HARNESS.token_service: record(days=3), HARNESS.secret_service: APP_SECRET}
        )
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.call(keychain, Endpoint(token_http_error(401), data={}))
        self.assertEqual(err.getvalue(), "ensemble-orchestrator: token-error: harness: HTTP 401\n")

    def test_a_dry_run_shows_each_token_once_as_first_read(self):
        cases = {
            record(days=4.24): {"days_left": 4.2},
            record(days=-1): {"state": "expired"},
            OLD: {"state": "no expiry"},
            None: {"state": "missing"},
        }
        for value, want in cases.items():
            with self.subTest(value=value):
                shown = []
                client.use(renew=False, show=lambda *seen, into=shown: into.append(seen))
                keychain = Keychain({} if value is None else {HARNESS.token_service: value})
                for _ in range(2):
                    with contextlib.suppress(types.KeychainError):
                        self.call(keychain, Endpoint(AssertionError("token request")))
                self.assertEqual(shown, [("harness", want)])
                self.assertEqual({argv[1] for argv, _ in keychain.calls}, {"find-generic-password"})

    def test_no_secret_or_token_leaks_from_any_path(self):
        echo = json.dumps({"error": APP_SECRET, "error_description": OLD})
        for days in (3, -1):
            for name, (answer, _) in self.failures(echo).items():
                with self.subTest(name, days=days):
                    found = self.reported()
                    keychain = Keychain(
                        {
                            HARNESS.token_service: record(days=days),
                            HARNESS.secret_service: APP_SECRET,
                        }
                    )
                    try:
                        self.call(keychain, Endpoint(answer))
                    except LinearError as error:
                        found.append(("error", str(error)))
                    texts = [text for _, text in found]
                    texts += [" ".join(argv) for argv, _ in keychain.calls]
                    self.assertTrue(texts)
                    self.assertEqual(leaks(texts), [])

    def test_graphql_errors_echoing_the_token_are_redacted(self):
        echo = json.dumps(
            {"errors": [{"message": f"bad {OLD}", "extensions": {"code": OLD}}]}
        ).encode()
        keychain = Keychain({HARNESS.token_service: record(days=20)})
        with (
            mock.patch("subprocess.run", keychain),
            mock.patch.object(urllib.request, "urlopen", return_value=respond(echo)),
            self.assertRaises(types.GraphQLErrors) as cm,
        ):
            client.linear_gql(VIEWER, identity=HARNESS)
        self.assertEqual(leaks([str(cm.exception), *client.messages(cm.exception.errors)]), [])
        self.assertEqual(client.messages(cm.exception.errors), ["bad [redacted] ([redacted])"])
        for code in (400,):
            with (
                mock.patch("subprocess.run", keychain),
                mock.patch.object(urllib.request, "urlopen", side_effect=http_error(code, echo)),
                self.assertRaises(LinearError) as cm,
            ):
                client.linear_gql(VIEWER, identity=HARNESS)
            self.assertEqual(leaks([str(cm.exception)]), [])
