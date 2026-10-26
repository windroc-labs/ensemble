package broker

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"slices"
	"strings"
	"testing"
	"time"

	"ensemble/broker/internal/github"
	"ensemble/broker/internal/protocol"
)

// recoveryStore persists bytes across lifecycle reconstruction. Failures are service-scoped:
// a broken backlog never prevents mutation of the original token record.
type recoveryStore struct {
	*secrets
	readFail, writeFail, deleteFail string
}

func (s *recoveryStore) Secret(service, account string) ([]byte, error) {
	if service == s.readFail {
		return nil, errLeaky
	}
	return s.secrets.Secret(service, account)
}

func (s *recoveryStore) PutSecret(service, account string, data []byte) error {
	if service == s.writeFail {
		return errLeaky
	}
	return s.secrets.PutSecret(service, account, data)
}

func (s *recoveryStore) DeleteSecret(service, account string) error {
	if service == s.deleteFail {
		return errLeaky
	}
	return s.secrets.DeleteSecret(service, account)
}

type recoveryAPI struct {
	now     time.Time
	fail    func(string) bool
	calls   []string
	revoked []string
	issued  int
}

func (a *recoveryAPI) RoundTrip(req *http.Request) (*http.Response, error) {
	status, body := http.StatusNoContent, ""
	switch {
	case req.Method == http.MethodGet && req.URL.Path == "/app/installations":
		status, body = http.StatusOK, `[{"id":1,"account":{"login":"acme"}}]`
	case req.Method == http.MethodDelete && req.URL.Path == "/installation/token":
		token := strings.TrimPrefix(req.Header.Get("Authorization"), "Bearer ")
		a.calls = append(a.calls, token)
		if a.fail != nil && a.fail(token) {
			status = http.StatusServiceUnavailable
		} else if slices.Contains(a.revoked, token) {
			status = http.StatusUnauthorized // a retry of confirmed invalid material is safe
		} else {
			a.revoked = append(a.revoked, token)
		}
	case req.Method == http.MethodPost && req.URL.Path == "/app/installations/1/access_tokens":
		a.issued++
		status = http.StatusCreated
		body = fmt.Sprintf(`{"token":"ghs_replacement_%d","expires_at":%q}`, a.issued, a.now.Add(time.Hour).Format(time.RFC3339))
	default:
		return nil, errors.New("unexpected fake GitHub request")
	}
	return &http.Response{StatusCode: status, Body: io.NopCloser(strings.NewReader(body)), Header: make(http.Header)}, nil
}

func recoveryLifecycle(t *testing.T, store *recoveryStore, remote *recoveryAPI) GitHub {
	t.Helper()
	return GitHub{
		Keychain: store,
		Apps:     keys{account: "1234567", key: testAppKey(t)},
		API:      github.Client{Base: "https://fake.invalid", HTTP: &http.Client{Transport: remote}, Now: func() time.Time { return remote.now }},
		Lock:     func() (func(), error) { return func() {}, nil },
	}
}

func recoverySeed(t *testing.T, entries ...tokenEntry) (*recoveryStore, *recoveryAPI) {
	t.Helper()
	s := &recoveryStore{secrets: &secrets{items: map[string][]byte{}}}
	b, err := json.Marshal(tokenRecord{Subject: "ensemble-bot", Tokens: entries})
	if err != nil {
		t.Fatal(err)
	}
	s.items[tokensService+"/"+ctr] = b
	return s, &recoveryAPI{now: clock}
}

func recoveryEntry(token string) tokenEntry {
	return tokenEntry{Account: "acme", Target: 1, Token: token, ExpiresAt: clock.Add(time.Hour).Format(time.RFC3339)}
}

func (s *recoveryStore) failBacklog(kind string) {
	if kind == "read" {
		s.readFail = revocationsService
	} else {
		s.writeFail = revocationsService
	}
}

func TestRevokeRecoveryAfterBacklogFailure(t *testing.T) {
	for _, kind := range []string{"read", "write"} {
		t.Run(kind, func(t *testing.T) {
			store, remote := recoverySeed(t, recoveryEntry("ghs_recovery"))
			store.failBacklog(kind)
			remote.fail = func(string) bool { return true }
			g := recoveryLifecycle(t, store, remote)
			wantCode(t, g.Release(context.Background(), ctr), protocol.KeychainFailed)
			if len(remote.calls) == 0 {
				t.Fatal("regression did not attempt the failed remote revoke")
			}

			// Only persisted bytes cross reconstruction, never the original entries.
			store.readFail, store.writeFail, remote.fail = "", "", nil
			rebuilt := recoveryLifecycle(t, store, remote)
			if err := rebuilt.Release(context.Background(), ctr); err != nil {
				t.Fatal(err)
			}
			if !slices.Contains(remote.revoked, "ghs_recovery") {
				t.Fatal("reconstructed lifecycle lost the unrevoked token after backlog " + kind + " failure")
			}
			if _, found := store.items[tokensService+"/"+ctr]; found {
				t.Fatal("completed recovery kept the original record")
			}
		})
	}
}

func TestDiscardRecoveryMatrix(t *testing.T) {
	for _, kind := range []string{"read", "write"} {
		for _, action := range []string{"whole", "replacement", "rollback", "partial"} {
			t.Run(kind+"/"+action, func(t *testing.T) {
				store, remote := recoverySeed(t, recoveryEntry("ghs_original"))
				if action == "partial" {
					store, remote = recoverySeed(t, recoveryEntry("ghs_confirmed"), recoveryEntry("ghs_original"))
				}
				store.failBacklog(kind)
				remote.fail = func(token string) bool { return token != "ghs_confirmed" }
				g := recoveryLifecycle(t, store, remote)
				switch action {
				case "replacement", "rollback":
					_, err := g.Refresh(context.Background(), "ensemble-bot", perms, ctr, func([]string, []byte) error {
						if action == "rollback" {
							return errors.New("fake injection failed")
						}
						return nil
					})
					if (err != nil) != (action == "rollback") {
						t.Fatalf("unexpected refresh outcome: %v", err)
					}
				default:
					wantCode(t, g.Release(context.Background(), ctr), protocol.KeychainFailed)
				}
				var rec tokenRecord
				if err := json.Unmarshal(store.items[tokensService+"/"+ctr], &rec); err != nil {
					t.Fatal("pending original record was lost")
				}
				want := 1
				if action == "replacement" || action == "rollback" {
					want = 2
				}
				if len(rec.Tokens) != want || slices.ContainsFunc(rec.Tokens, func(e tokenEntry) bool { return e.Token == "ghs_confirmed" }) {
					t.Fatal("record did not retain exactly the unconfirmed tokens")
				}
				store.readFail, store.writeFail, remote.fail = "", "", nil
				if err := recoveryLifecycle(t, store, remote).Release(context.Background(), ctr); err != nil {
					t.Fatal(err)
				}
				for _, e := range rec.Tokens {
					if !slices.Contains(remote.revoked, e.Token) {
						t.Fatal("reconstructed lifecycle failed to revoke retained material")
					}
				}
			})
		}
	}
}

func TestRevokeRecoveryAfterDeleteFailure(t *testing.T) {
	for _, backlogBroken := range []bool{false, true} {
		t.Run(fmt.Sprint(backlogBroken), func(t *testing.T) {
			store, remote := recoverySeed(t, recoveryEntry("ghs_recovery"))
			if backlogBroken {
				store.readFail = revocationsService
			}
			store.deleteFail = tokensService
			wantCode(t, recoveryLifecycle(t, store, remote).Release(context.Background(), ctr), protocol.KeychainFailed)
			if !slices.Contains(remote.revoked, "ghs_recovery") {
				t.Fatal("record delete failure prevented remote revocation")
			}
			if _, found := store.items[tokensService+"/"+ctr]; !found {
				t.Fatal("failed deletion lost the persisted recovery record")
			}
			store.readFail, store.deleteFail = "", ""
			if err := recoveryLifecycle(t, store, remote).Release(context.Background(), ctr); err != nil {
				t.Fatal(err)
			}
			if _, found := store.items[tokensService+"/"+ctr]; found {
				t.Fatal("repeated revoke failed to finish record deletion")
			}
		})
	}
}

func TestDiscardExpiryConfirmation(t *testing.T) {
	for _, deadline := range []string{clock.Add(time.Hour).Format(time.RFC3339), "invalid"} {
		t.Run(deadline, func(t *testing.T) {
			e := recoveryEntry("ghs_recovery")
			e.ExpiresAt = deadline
			store, remote := recoverySeed(t, e)
			store.readFail = revocationsService
			remote.fail = func(string) bool { return true }
			wantCode(t, recoveryLifecycle(t, store, remote).Release(context.Background(), ctr), protocol.KeychainFailed)
			if _, found := store.items[tokensService+"/"+ctr]; !found {
				t.Fatal("live or unknown-expiry token lost its recovery record")
			}
			remote.now = clock.Add(time.Hour)
			remote.calls = nil
			err := recoveryLifecycle(t, store, remote).Release(context.Background(), ctr)
			if deadline == "invalid" {
				wantCode(t, err, protocol.KeychainFailed)
				if _, found := store.items[tokensService+"/"+ctr]; !found {
					t.Fatal("invalid expiry was treated as confirmed expiry")
				}
			} else if err != nil || len(remote.calls) != 0 {
				t.Fatalf("confirmed expiry should need no remote revoke: %v, calls=%d", err, len(remote.calls))
			} else if _, found := store.items[tokensService+"/"+ctr]; found {
				t.Fatal("confirmed expiry did not retire the record")
			}
		})
	}
}

func TestRetryInvalidExpiryStaysTracked(t *testing.T) {
	store, remote := recoverySeed(t)
	b, err := json.Marshal([]pending{{Container: ctr, Account: "acme", Token: "ghs_recovery", ExpiresAt: "invalid"}})
	if err != nil {
		t.Fatal(err)
	}
	store.items[revocationsService+"/"+revocationsAccount] = b
	remote.fail = func(string) bool { return true }
	got, err := recoveryLifecycle(t, store, remote).Retry(context.Background())
	if err != nil || len(got) != 1 || len(remote.calls) != 1 {
		t.Fatalf("invalid expiry must remain pending after failed revoke: pending=%d calls=%d err=%v", len(got), len(remote.calls), err)
	}
}

func TestIssueRollbackWithUnreadableRecord(t *testing.T) {
	for _, revokeFails := range []bool{false, true} {
		t.Run(fmt.Sprint(revokeFails), func(t *testing.T) {
			store, remote := recoverySeed(t, recoveryEntry("ghs_existing"))
			original := slices.Clone(store.items[tokensService+"/"+ctr])
			store.readFail = tokensService
			remote.fail = func(string) bool { return revokeFails }
			g := recoveryLifecycle(t, store, remote)
			_, err := g.Issue(context.Background(), "ensemble-bot", perms, ctr, func([]string, []byte) error {
				t.Fatal("an unrecorded token must never reach the container")
				return nil
			})
			wantCode(t, err, protocol.KeychainFailed)
			if !slices.Equal(store.items[tokensService+"/"+ctr], original) {
				t.Fatal("rollback changed the unreadable original record")
			}
			if revokeFails {
				var backlog []pending
				if err := json.Unmarshal(store.items[revocationsService+"/"+revocationsAccount], &backlog); err != nil ||
					len(backlog) != 1 || backlog[0].Token != "ghs_replacement_1" {
					t.Fatal("unrecorded issued token was not durably queued after failed revoke")
				}
			} else if !slices.Contains(remote.revoked, "ghs_replacement_1") {
				t.Fatal("unrecorded issued token was not revoked after record read failure")
			}
			store.readFail, remote.fail = "", nil
			if err := recoveryLifecycle(t, store, remote).Release(context.Background(), ctr); err != nil {
				t.Fatal(err)
			}
			for _, token := range []string{"ghs_existing", "ghs_replacement_1"} {
				if !slices.Contains(remote.revoked, token) {
					t.Fatal("reconstructed lifecycle could not finish issuance rollback")
				}
			}
		})
	}
}
