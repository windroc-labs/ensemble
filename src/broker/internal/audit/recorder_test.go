package audit

import (
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"

	"ensemble/broker/internal/protocol"
)

const other = "ensemble-worker_2-fedcba9876543210fedcba9876543210"

type sink struct {
	lines [][]byte
	err   error
	calls int
}

func (s *sink) Append(lines ...[]byte) error {
	s.calls++
	s.lines = append(s.lines, lines...)
	return s.err
}

func (s *sink) decoded(t *testing.T) []map[string]any {
	t.Helper()
	var out []map[string]any
	for _, l := range s.lines {
		var v map[string]any
		if err := json.Unmarshal(l, &v); err != nil {
			t.Fatal(err)
		}
		out = append(out, v)
	}
	return out
}

func recorder(command, container string) (*Recorder, *sink) {
	s := &sink{}
	return &Recorder{Command: command, Container: container, Log: s,
		Now: func() time.Time { return time.Date(2026, 10, 17, 12, 0, 0, 0, time.UTC) }}, s
}

func results(line map[string]any) []string {
	var out []string
	for _, c := range line["credentials"].([]any) {
		out = append(out, c.(map[string]any)["result"].(string))
	}
	return out
}

func equal(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func TestNilRecorder(t *testing.T) {
	var r *Recorder
	r.Add(ctr, Credential{Kind: GitHubToken, Result: Issued}, "k")
	r.Flush(nil)
}

func TestRouting(t *testing.T) {
	r, s := recorder("close", ctr)
	gh := func(result string) Credential { return Credential{Kind: GitHubToken, Result: result, Account: "acme"} }
	r.Add(ctr, gh(Pending), "own")
	r.Add(other, gh(Pending), "o1")
	r.Add(other, gh(Revoked), "o2")
	r.Add(other, gh(ExpiredUnrevoked), "o3")
	r.Add(ctr, gh(Revoked), "own")
	r.Add(ctr, Credential{Kind: ClaudeSetupToken, Result: Withdrawn}, "")
	r.Flush(nil)
	lines := s.decoded(t)
	if len(lines) != 2 || s.calls != 1 {
		t.Fatalf("one append of the retry line, then the command line: %d lines, %d calls", len(lines), s.calls)
	}
	retry, own := lines[0], lines[1]
	if retry["event"] != "revocation-retry" || retry["command"] != "close" || retry["container"] != other || retry["session"] != "worker_2" {
		t.Fatal(retry)
	}
	if !equal(results(retry), []string{Revoked, ExpiredUnrevoked}) {
		t.Fatalf("another container keeps only revoked and expired outcomes: %v", results(retry))
	}
	if own["event"] != "close" || own["session"] != "worker_1" || own["error"] != nil {
		t.Fatal(own)
	}
	if !equal(results(own), []string{Revoked, Withdrawn}) {
		t.Fatalf("a later outcome replaces the token's pending entry: %v", results(own))
	}
}

func TestKeyedOutcomesKeepHistory(t *testing.T) {
	r, s := recorder("start", ctr)
	r.Add(ctr, Credential{Kind: GitHubToken, Result: Issued}, "k")
	r.Add(ctr, Credential{Kind: GitHubToken, Result: Pending, Reason: "queued"}, "k")
	r.Add(ctr, Credential{Kind: GitHubToken, Result: Revoked}, "k")
	r.Add(ctr, Credential{Kind: GitHubToken, Result: Revoked}, "")
	r.Flush(nil)
	if got := results(s.decoded(t)[0]); !equal(got, []string{Issued, Revoked, Revoked}) {
		t.Fatalf("issued stays; pending is replaced: %v", got)
	}
}

func TestNoContainerYet(t *testing.T) {
	r, s := recorder("start", "")
	r.Add("", Credential{Kind: GitHubToken, Result: Revoked}, "")
	r.Add(other, Credential{Kind: GitHubToken, Result: Revoked}, "")
	r.Flush(protocol.New(protocol.ManifestRejected, "Manifest rejected: x"))
	lines := s.decoded(t)
	if len(lines) != 2 || lines[0]["container"] != other {
		t.Fatalf("%v", lines)
	}
	if lines[1]["container"] != nil || lines[1]["error"] != "manifest_rejected" || len(results(lines[1])) != 0 {
		t.Fatal(lines[1])
	}
}

func TestFlushError(t *testing.T) {
	r, s := recorder("refresh", ctr)
	r.Flush(errors.New("opaque"))
	if s.decoded(t)[0]["error"] != "internal_error" {
		t.Fatal(s.decoded(t)[0])
	}
	r.Flush(nil)
	if s.calls != 1 {
		t.Fatal("Flush runs once")
	}
}

func TestStatusWritesOnlyRetryLines(t *testing.T) {
	for _, command := range []string{"status", "build", "workspace-rm"} {
		r, s := recorder(command, "")
		r.Flush(nil)
		if s.calls != 0 {
			t.Fatalf("%s wrote %d lines", command, len(s.lines))
		}
	}
	r, s := recorder("status", "")
	r.Add(other, Credential{Kind: GitHubToken, Result: Pending}, "")
	r.Add(other, Credential{Kind: GitHubToken, Result: Revoked}, "")
	r.Flush(nil)
	if lines := s.decoded(t); len(lines) != 1 || lines[0]["event"] != "revocation-retry" {
		t.Fatalf("%v", lines)
	}
}

func TestFlushReportsAppendError(t *testing.T) {
	r, s := recorder("close", ctr)
	s.err = errors.New("disk full")
	if err := r.Flush(nil); !errors.Is(err, s.err) || s.calls != 1 {
		t.Fatal(s.calls)
	}
	if err := r.Flush(nil); err != nil || s.calls != 1 {
		t.Fatal("a second flush must not retry or create another warning")
	}
}

func TestFlushNoWriteHasNoError(t *testing.T) {
	var r *Recorder
	if err := r.Flush(nil); err != nil {
		t.Fatal(err)
	}
	r, s := recorder("status", "")
	s.err = errors.New("must not be called")
	if err := r.Flush(nil); err != nil || s.calls != 0 {
		t.Fatal("empty flush attempted a write")
	}
}

func TestNoticeLines(t *testing.T) {
	for _, command := range []string{"login", "status", "notices", "refresh"} {
		r, s := recorder(command, "")
		if command == "notices" || command == "refresh" {
			r.Begin(ctr, nil)
		}
		expires := "2026-11-20T00:00:00Z"
		r.Notice(r.Container, Credential{Kind: EnvSecret, Item: "ensemble-broker-env/db", Result: Notified, Notice: "30d", ExpiresAt: &expires})
		r.Notice(r.Container, Credential{Kind: OpencodeAPIKey, Item: "ensemble-broker-opencode/strata", Result: Notified, Notice: "rotated"})
		if err := r.Flush(nil); err != nil {
			t.Fatal(err)
		}
		lines := s.decoded(t)
		if command == "refresh" {
			lines = lines[:2] // then the command's own line
		}
		if len(lines) != 2 {
			t.Fatalf("%s: %v", command, lines)
		}
		for i, want := range []string{
			`{"kind":"env-secret","result":"notified","expires_at":"2026-11-20T00:00:00Z","item":"ensemble-broker-env/db","notice":"30d"}`,
			`{"kind":"opencode-api-key","result":"notified","expires_at":null,"item":"ensemble-broker-opencode/strata","notice":"rotated"}`,
		} {
			line := lines[i]
			if line["event"] != "credential-notice" || line["command"] != command || !strings.Contains(string(s.lines[i]), `"credentials":[`+want+`]`) {
				t.Errorf("%s %d: %s", command, i, s.lines[i])
			}
			if (r.Container == "") != (line["container"] == nil) {
				t.Errorf("%s: container %v", command, line["container"])
			}
		}
	}
	var r *Recorder
	r.Notice("", Credential{Kind: EnvSecret, Result: Notified})
}
