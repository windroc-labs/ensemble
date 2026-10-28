package broker

import (
	"context"
	"encoding/json"
	"fmt"
	"slices"
	"strings"
	"testing"
	"time"

	"ensemble/broker/internal/audit"
)

type lines struct{ got [][]byte }

func (l *lines) Append(data ...[]byte) error { l.got = append(l.got, data...); return nil }

// audited gives r's GitHub a recorder for command on container c.
func (r *rig) audited(command, c string) (*audit.Recorder, *lines) {
	sink := &lines{}
	rec := &audit.Recorder{Command: command, Container: c, Log: sink, Now: func() time.Time { return clock }}
	r.g.Audit = rec
	return rec, sink
}

type logLine struct {
	Event       string `json:"event"`
	Container   string `json:"container"`
	Session     string `json:"session"`
	Credentials []struct {
		Kind         string    `json:"kind"`
		Result       string    `json:"result"`
		Reason       string    `json:"reason"`
		Account      string    `json:"account"`
		Installation int64     `json:"installation"`
		ExpiresAt    string    `json:"expires_at"` // "": null
		Selection    *string   `json:"repository_selection"`
		Repositories *[]string `json:"repositories"`
		ReposError   string    `json:"repositories_error"`
	} `json:"credentials"`
}

func flush(t *testing.T, rec *audit.Recorder, sink *lines) []logLine {
	t.Helper()
	rec.Flush(nil)
	var out []logLine
	for _, l := range sink.got {
		if strings.Contains(string(l), "ghs_") || strings.Contains(string(l), "SECRET") {
			t.Fatalf("line holds a token: %s", l)
		}
		var v logLine
		if err := json.Unmarshal(l, &v); err != nil {
			t.Fatal(err)
		}
		out = append(out, v)
	}
	return out
}

func resultsOf(l logLine) []string {
	var out []string
	for _, c := range l.Credentials {
		out = append(out, c.Result)
	}
	return out
}

func TestIssueAudit(t *testing.T) {
	r := newRig(t, "acme", "other")
	rec, sink := r.audited("start", ctr)
	issued(t, r)
	got := flush(t, rec, sink)
	if len(got) != 1 || len(got[0].Credentials) != 2 {
		t.Fatalf("%+v", got)
	}
	for i, login := range []string{"acme", "other"} {
		c := got[0].Credentials[i]
		if c.Kind != audit.GitHubToken || c.Result != audit.Issued || c.Account != login || c.Installation != int64(i+1) ||
			c.ExpiresAt != "2026-10-17T13:00:00Z" || c.Selection == nil || *c.Selection != "all" ||
			c.Repositories == nil || !slices.Equal(*c.Repositories, []string{login + "/one", login + "/two"}) {
			t.Fatalf("%d: %+v", i, c)
		}
	}
}

func TestIssueAuditRepositoriesFail(t *testing.T) {
	r := newRig(t, "acme")
	r.api.reposFail = 500
	rec, sink := r.audited("start", ctr)
	commands := issued(t, r)
	if len(commands) != 1 {
		t.Fatal("a failed listing stops the issue")
	}
	c := flush(t, rec, sink)[0].Credentials[0]
	if c.Result != audit.Issued || c.Selection != nil || c.Repositories != nil || c.ReposError != "GitHub answered HTTP 500 to GET /installation/repositories" {
		t.Fatalf("%+v", c)
	}
}

func TestIssueFailureAudit(t *testing.T) {
	r := newRig(t, "acme", "other")
	r.api.issueFail = map[int]int{2: 500}
	rec, sink := r.audited("start", ctr)
	if _, _, _, err := r.g.Issue(context.Background(), "ensemble-bot", "", perms, ctr); err == nil {
		t.Fatal("no error")
	}
	if got := resultsOf(flush(t, rec, sink)[0]); !slices.Equal(got, []string{audit.Issued, audit.Revoked}) {
		t.Fatalf("the rolled-back token is issued, then revoked: %v", got)
	}
}

func TestRefreshAudit(t *testing.T) {
	r := newRig(t, "acme")
	issued(t, r)
	rec, sink := r.audited("refresh", ctr)
	if _, err := r.g.Refresh(context.Background(), "ensemble-bot", "", perms, ctr, r.exec.run); err != nil {
		t.Fatal(err)
	}
	got := flush(t, rec, sink)[0]
	if !slices.Equal(resultsOf(got), []string{audit.Renewed}) || got.Credentials[0].Repositories == nil || got.Credentials[0].Installation != 1 {
		t.Fatalf("%+v", got)
	}
}

// opaque is a token GitHub could issue in another shape: only the bearer redaction keeps it out of the audit.
func opaque(id string, n int) string { return fmt.Sprintf("opaqueSECRET%s0%d", id, n) }

// AC7: a failed in-command revocation is audited unrevoked with its reason and expiry, without the token, and leaves
// the command's result as it was.
func TestFailedRevocationAuditedUnrevoked(t *testing.T) {
	const echo = "Bad credentials for %s"
	unrevoked := func(t *testing.T, l logLine, reason, expires string) {
		t.Helper()
		c := l.Credentials[len(l.Credentials)-1]
		if c.Result != audit.Unrevoked || c.Reason != reason || c.ExpiresAt != expires || c.Account != "acme" {
			t.Fatalf("%+v", c)
		}
	}
	t.Run("start rollback", func(t *testing.T) {
		r := newRig(t, "acme")
		r.api.tokenOf, r.api.revokeMsg = opaque, echo
		r.api.revokeErr = func(string) int { return 500 }
		rec, sink := r.audited("start", ctr)
		_, revoke, _, err := r.g.Issue(context.Background(), "ensemble-bot", "", perms, ctr)
		if err != nil {
			t.Fatal(err)
		}
		revoke()
		got := flush(t, rec, sink)[0]
		if !slices.Equal(resultsOf(got), []string{audit.Issued, audit.Unrevoked}) {
			t.Fatalf("%+v", got)
		}
		unrevoked(t, got, `GitHub answered HTTP 500 to DELETE /installation/token: "Bad credentials for [redacted]"`, "2026-10-17T13:00:00Z")
		r.noKeychain(t)
	})
	t.Run("start rollback offline", func(t *testing.T) {
		r := newRig(t, "acme")
		rec, sink := r.audited("start", ctr)
		_, revoke, _, err := r.g.Issue(context.Background(), "ensemble-bot", "", perms, ctr)
		if err != nil {
			t.Fatal(err)
		}
		r.srv.Close()
		revoke()
		unrevoked(t, flush(t, rec, sink)[0], "GitHub unreachable: DELETE /installation/token", "2026-10-17T13:00:00Z")
	})
	t.Run("close", func(t *testing.T) {
		r := newRig(t, "acme")
		r.api.revokeMsg = echo
		r.api.revokeErr = func(string) int { return 502 }
		rec, sink := r.audited("close", ctr)
		mtime := clock.Add(-10 * time.Minute)
		r.g.Revoke(context.Background(), ctr, files(fmt.Sprintf("acme %d ghs_ClosedTok1\n", mtime.Unix())))
		unrevoked(t, flush(t, rec, sink)[0], `GitHub answered HTTP 502 to DELETE /installation/token: "Bad credentials for [redacted]"`,
			mtime.Add(time.Hour).Format(time.RFC3339))
		r.noKeychain(t)
	})
	t.Run("prefetch", func(t *testing.T) {
		r, a := prefetchRig(t, installed())
		r.api.tokenOf, r.api.revokeMsg = opaque, echo
		r.api.revokeErr = func(string) int { return 500 }
		if err := r.g.Prefetch(context.Background(), "acme/app", "main", "/w", ctr, r.exec.run); err != nil {
			t.Fatalf("a failed revocation failed the prefetch: %v", err)
		}
		got := flush(t, a.rec, a.sink)[0]
		if !slices.Equal(resultsOf(got), []string{audit.Issued, audit.Unrevoked}) {
			t.Fatalf("%+v", got)
		}
		unrevoked(t, got, `GitHub answered HTTP 500 to DELETE /installation/token: "Bad credentials for [redacted]"`, "2026-10-17T13:00:00Z")
		r.noKeychain(t)
	})
}
