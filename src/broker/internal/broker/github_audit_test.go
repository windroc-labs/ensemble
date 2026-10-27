package broker

import (
	"context"
	"encoding/json"
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
		ExpiresAt    string    `json:"expires_at"`
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
		if strings.Contains(string(l), "ghs_") {
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
	issued(t, r)
	if _, ok := r.record(t); !ok {
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
	if _, err := r.g.Issue(context.Background(), "ensemble-bot", perms, ctr, r.exec.run); err == nil {
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
	if _, err := r.g.Refresh(context.Background(), "ensemble-bot", perms, ctr, r.exec.run); err != nil {
		t.Fatal(err)
	}
	got := flush(t, rec, sink)[0]
	if !slices.Equal(resultsOf(got), []string{audit.Renewed, audit.Revoked}) || got.Credentials[0].Repositories == nil {
		t.Fatalf("%+v", got)
	}

	r = newRig(t, "acme")
	issued(t, r)
	r.api.revokeErr = func(token string) int {
		if token == "ghs_1_1" {
			return 502
		}
		return 0
	}
	rec, sink = r.audited("refresh", ctr)
	if _, err := r.g.Refresh(context.Background(), "ensemble-bot", perms, ctr, r.exec.run); err != nil {
		t.Fatal(err)
	}
	got = flush(t, rec, sink)[0]
	if !slices.Equal(resultsOf(got), []string{audit.Renewed, audit.Pending}) || got.Credentials[1].Reason != "GitHub answered HTTP 502 to DELETE /installation/token" {
		t.Fatalf("the old token stays pending with the retry's reason: %+v", got)
	}
}

func TestRevokeAudit(t *testing.T) {
	r := newRig(t, "acme", "other")
	issued(t, r)
	rec, sink := r.audited("close", ctr)
	if err := r.g.Release(context.Background(), ctr); err != nil {
		t.Fatal(err)
	}
	if got := resultsOf(flush(t, rec, sink)[0]); !slices.Equal(got, []string{audit.Revoked, audit.Revoked}) {
		t.Fatal(got)
	}
}

func TestRevokeAuditOffline(t *testing.T) {
	r := newRig(t, "acme", "other")
	issued(t, r)
	rec, sink := r.audited("close", ctr)
	r.srv.Close()
	if err := r.g.Release(context.Background(), ctr); err != nil {
		t.Fatal(err)
	}
	got := flush(t, rec, sink)[0]
	if !slices.Equal(resultsOf(got), []string{audit.Pending, audit.Pending}) {
		t.Fatal(resultsOf(got))
	}
	for _, c := range got.Credentials {
		if !strings.HasPrefix(c.Reason, "GitHub unreachable") || c.ExpiresAt == "" || c.Account == "" {
			t.Fatalf("%+v", c)
		}
	}
}

func TestRevokeAuditLockFails(t *testing.T) {
	r := newRig(t, "acme")
	issued(t, r)
	r.api.revokeErr = func(string) int { return 500 }
	r.lockFails(0)
	rec, sink := r.audited("close", ctr)
	_ = r.g.Release(context.Background(), ctr)
	got := flush(t, rec, sink)[0]
	if !slices.Equal(resultsOf(got), []string{audit.Pending}) || got.Credentials[0].Reason != "GitHub answered HTTP 500 to DELETE /installation/token" {
		t.Fatalf("a direct revoke that fails, then is queued: %+v", got)
	}
}

// flakySecrets fails reads of service after the first ok.
type flakySecrets struct {
	*secrets
	service string
	ok, n   int
}

func (f *flakySecrets) Secret(service, account string) ([]byte, error) {
	if service == f.service {
		if f.n++; f.n > f.ok {
			return nil, errLeaky
		}
	}
	return f.secrets.Secret(service, account)
}

func TestDiscardRecordsQueuedTokens(t *testing.T) {
	r := newRig(t, "acme", "other")
	issued(t, r)
	r.g.Keychain = &flakySecrets{secrets: r.keys, service: revocationsService, ok: 1}
	rec, sink := r.audited("close", ctr)
	if err := r.g.Release(context.Background(), ctr); err != nil {
		t.Fatal(err)
	}
	got := flush(t, rec, sink)[0]
	if !slices.Equal(resultsOf(got), []string{audit.Pending, audit.Pending}) || got.Credentials[0].Reason != "queued for revocation" {
		t.Fatalf("queued tokens stay on the line when the retry cannot read the backlog: %+v", got)
	}
}

func TestListingFollowsTheRecord(t *testing.T) {
	r := newRig(t, "acme", "other")
	var unrecorded []string
	r.api.onRepos = func(token string) {
		rec, _ := r.record(t)
		if !slices.ContainsFunc(rec.Tokens, func(e tokenEntry) bool { return e.Token == token }) {
			unrecorded = append(unrecorded, token)
		}
	}
	r.audited("start", ctr)
	issued(t, r)
	if _, err := r.g.Refresh(context.Background(), "ensemble-bot", perms, ctr, r.exec.run); err != nil {
		t.Fatal(err)
	}
	if r.api.issued != 4 || len(unrecorded) != 0 {
		t.Fatalf("listed %v before recording them", unrecorded)
	}
}

func TestUnrecordedTokenAudit(t *testing.T) {
	r := newRig(t, "acme")
	r.lockFails(0)
	rec, sink := r.audited("start", ctr)
	if _, err := r.g.Issue(context.Background(), "ensemble-bot", perms, ctr, r.exec.run); err == nil {
		t.Fatal("no error")
	}
	got := flush(t, rec, sink)[0]
	if !slices.Equal(resultsOf(got), []string{audit.Issued, audit.Revoked}) || got.Credentials[0].ReposError != "not listed: the token could not be recorded" {
		t.Fatalf("%+v", got)
	}
}
