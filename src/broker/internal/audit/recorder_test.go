package audit

import (
	"encoding/json"
	"errors"
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
	r.Add(ctr, Credential{Kind: GitHubToken, Result: Issued})
	if err := r.Flush(nil); err != nil {
		t.Fatal(err)
	}
}

func TestRouting(t *testing.T) {
	r, s := recorder("close", ctr)
	gh := func(result string) Credential { return Credential{Kind: GitHubToken, Result: result, Account: "acme"} }
	r.Add(ctr, gh(Issued))
	r.Add(other, gh(Revoked))
	r.Add(ctr, gh(Unrevoked))
	r.Add(ctr, gh(Revoked))
	r.Add(ctr, Credential{Kind: ClaudeSetupToken, Result: Withdrawn})
	r.Flush(nil)
	lines := s.decoded(t)
	if len(lines) != 1 || s.calls != 1 {
		t.Fatalf("only the command line: %d lines, %d calls", len(lines), s.calls)
	}
	own := lines[0]
	if own["event"] != "close" || own["session"] != "worker_1" || own["error"] != nil {
		t.Fatal(own)
	}
	if !equal(results(own), []string{Issued, Unrevoked, Revoked, Withdrawn}) {
		t.Fatalf("every own outcome kept in order, another container's dropped: %v", results(own))
	}
}

func TestNoContainerYet(t *testing.T) {
	r, s := recorder("start", "")
	r.Add("", Credential{Kind: GitHubToken, Result: Revoked})
	r.Add(other, Credential{Kind: GitHubToken, Result: Revoked})
	r.Flush(protocol.New(protocol.ManifestRejected, "Manifest rejected: x"))
	lines := s.decoded(t)
	if len(lines) != 1 || lines[0]["container"] != nil || lines[0]["error"] != "manifest_rejected" || len(results(lines[0])) != 0 {
		t.Fatalf("%v", lines)
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

func TestStatusWritesNothing(t *testing.T) {
	for _, command := range []string{"status", "build", "workspace-rm"} {
		r, s := recorder(command, "")
		r.Add(other, Credential{Kind: GitHubToken, Result: Revoked})
		if err := r.Flush(nil); err != nil || s.calls != 0 {
			t.Fatalf("%s wrote %d lines: %v", command, len(s.lines), err)
		}
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
