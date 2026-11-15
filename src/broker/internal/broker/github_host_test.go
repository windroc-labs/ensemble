package broker

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"slices"
	"strings"
	"testing"

	"ensemble/broker/internal/audit"
	"ensemble/broker/internal/protocol"
)

const hostPermsBody = `{"permissions":{"actions":"read","checks":"read","contents":"write","metadata":"read","pull_requests":"write","statuses":"read","workflows":"write"}}`

// hostRig is a rig whose App key item is the host App's, auditing host-gh-login.
func hostRig(t *testing.T, installs ...string) (*rig, *audit.Recorder, *lines) {
	t.Helper()
	r := newRig(t)
	r.g.Apps = keys{app: HostAppKeyService, account: "1234567", key: testAppKey(t)}
	r.api.installs = installs
	rec, sink := r.audited("host-gh-login", HostContainer)
	return r, rec, sink
}

// hostLine is the one audit line host-gh-login writes, with its error code.
func hostLine(t *testing.T, rec *audit.Recorder, sink *lines, err error) (logLine, string) {
	t.Helper()
	if rec.Flush(err) != nil || len(sink.got) != 1 {
		t.Fatalf("audit lines %q", sink.got)
	}
	if strings.Contains(string(sink.got[0]), "ghs_") {
		t.Fatalf("line holds a token: %s", sink.got[0])
	}
	var line logLine
	var code struct {
		Command string `json:"command"`
		Error   string `json:"error"`
	}
	if json.Unmarshal(sink.got[0], &line) != nil || json.Unmarshal(sink.got[0], &code) != nil {
		t.Fatalf("line %s", sink.got[0])
	}
	if line.Event != "refresh" || line.Container != HostContainer || code.Command != "host-gh-login" {
		t.Fatalf("line %s", sink.got[0])
	}
	return line, code.Error
}

func TestHostLoginIssuesOneInstallationTokenToGHStdin(t *testing.T) {
	r, rec, sink := hostRig(t, "windroc-labs")
	var given, held []byte
	expiry, err := r.g.HostLogin(context.Background(), func(token []byte) error {
		given, held = slices.Clone(token), token
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if string(given) != tok(1, 1) || !slices.Equal(r.api.bodies, []string{hostPermsBody}) {
		t.Fatalf("login got %q; requested %q", given, r.api.bodies)
	}
	if strings.Trim(string(held), "\x00") != "" {
		t.Fatalf("the login's token buffer is not erased: %q", held)
	}
	if expiry != (protocol.Expiry{Account: "windroc-labs", ExpiresAt: "2026-10-17T13:00:00Z"}) {
		t.Fatalf("expiry %+v", expiry)
	}
	if len(r.api.revoked) != 0 {
		t.Fatalf("revoked %q", r.api.revoked)
	}
	line, code := hostLine(t, rec, sink, err)
	if code != "" || len(line.Credentials) != 1 {
		t.Fatalf("line %+v error %q", line, code)
	}
	if c := line.Credentials[0]; c.Kind != audit.GitHubToken || c.Result != audit.Issued || c.Account != "windroc-labs" || c.Installation != 1 {
		t.Fatalf("credential %+v", c)
	}
	r.noKeychain(t)
}

func TestHostLoginNeedsExactlyOneInstallation(t *testing.T) {
	for _, installs := range [][]string{nil, {"windroc", "windroc-labs"}} {
		r, rec, sink := hostRig(t, installs...)
		called := false
		_, err := r.g.HostLogin(context.Background(), func([]byte) error { called = true; return nil })
		wantCode(t, err, protocol.GitHubFailed)
		if !strings.Contains(err.Error(), "exactly one installation") || called || r.api.issued != 0 {
			t.Fatalf("%q: %v called=%v issued=%d", installs, err, called, r.api.issued)
		}
		if _, code := hostLine(t, rec, sink, err); code != string(protocol.GitHubFailed) {
			t.Fatalf("%q: line error %q", installs, code)
		}
	}
}

func TestHostLoginWithoutTheHostAppKeyNamesItsLogin(t *testing.T) {
	r, _, _ := hostRig(t, "windroc-labs")
	r.g.Apps = keys{account: "1234567", key: testAppKey(t)} // only the Docker App's key
	_, err := r.g.HostLogin(context.Background(), func([]byte) error { return nil })
	wantCode(t, err, protocol.LoginRequired)
	if !strings.Contains(err.Error(), "ensemble-broker login github ensemble-broker-host-bot") || r.api.issued != 0 {
		t.Fatalf("%v issued=%d", err, r.api.issued)
	}
}

// A login that fails, or leaves the token outside the keyring, revokes it; its error never holds the token.
func TestHostLoginRevokesTheTokenAFailedLoginGot(t *testing.T) {
	r, rec, sink := hostRig(t, "windroc-labs")
	_, err := r.g.HostLogin(context.Background(), func(token []byte) error {
		return errors.New("gh auth login: exit status 1: bad token " + string(token))
	})
	wantCode(t, err, protocol.GitHubFailed)
	if !strings.Contains(err.Error(), "exit status 1: bad token [redacted]") {
		t.Fatalf("%v", err)
	}
	if !slices.Equal(r.api.revoked, []string{tok(1, 1)}) {
		t.Fatalf("revoked %q", r.api.revoked)
	}
	line, code := hostLine(t, rec, sink, err)
	if code != string(protocol.GitHubFailed) || !slices.Equal(resultsOf(line), []string{audit.Issued, audit.Revoked}) {
		t.Fatalf("line %+v error %q", line, code)
	}
}

func TestHostLoginNamesATokenItCouldNotRevoke(t *testing.T) {
	r, rec, sink := hostRig(t, "windroc-labs")
	r.api.revokeErr = func(string) int { return http.StatusBadGateway }
	_, err := r.g.HostLogin(context.Background(), func([]byte) error { return errors.New("no keyring") })
	wantCode(t, err, protocol.GitHubFailed)
	if !strings.Contains(err.Error(), "not revoked") || !strings.Contains(err.Error(), "2026-10-17T13:00:00Z") {
		t.Fatalf("%v", err)
	}
	line, _ := hostLine(t, rec, sink, err)
	if !slices.Equal(resultsOf(line), []string{audit.Issued, audit.Unrevoked}) {
		t.Fatalf("line %+v", line)
	}
}
