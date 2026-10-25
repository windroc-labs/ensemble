package audit

import (
	"encoding/json"
	"testing"
	"time"
)

const ctr = "ensemble-worker_1-0123456789abcdef0123456789abcdef"

func ptr(s string) *string { return &s }

func encode(t *testing.T, v any) map[string]any {
	t.Helper()
	data, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	var out map[string]any
	if err := json.Unmarshal(data, &out); err != nil {
		t.Fatal(err)
	}
	return out
}

func keys(m map[string]any) map[string]bool {
	out := map[string]bool{}
	for k := range m {
		out[k] = true
	}
	return out
}

func sameKeys(t *testing.T, got map[string]any, want ...string) {
	t.Helper()
	k := keys(got)
	if len(k) != len(want) {
		t.Fatalf("keys %v, want %v", k, want)
	}
	for _, w := range want {
		if !k[w] {
			t.Fatalf("keys %v, want %v", k, want)
		}
	}
}

func TestCredentialJSON(t *testing.T) {
	claude := encode(t, Credential{Kind: ClaudeSetupToken, Result: Issued})
	sameKeys(t, claude, "kind", "result", "expires_at")
	if claude["expires_at"] != nil || claude["kind"] != "claude-setup-token" || claude["result"] != "issued" {
		t.Fatal(claude)
	}

	selection := "selected"
	gh := encode(t, Credential{Kind: GitHubToken, Result: Issued, ExpiresAt: ptr("2026-10-17T13:00:00Z"), Account: "acme",
		Installation: 7, Repos: &Repositories{Selection: &selection, Names: []string{"acme/a", "acme/b"}}})
	sameKeys(t, gh, "kind", "result", "expires_at", "account", "installation", "repository_selection", "repositories")
	if gh["repository_selection"] != "selected" || len(gh["repositories"].([]any)) != 2 || gh["installation"] != 7.0 {
		t.Fatal(gh)
	}

	empty := encode(t, Credential{Kind: GitHubToken, Result: Renewed, Account: "acme", Installation: 7,
		Repos: &Repositories{Selection: &selection}})
	if list, ok := empty["repositories"].([]any); !ok || len(list) != 0 {
		t.Fatalf("an installation without repositories lists [], got %v", empty["repositories"])
	}

	failed := encode(t, Credential{Kind: GitHubToken, Result: Issued, Account: "acme", Installation: 7,
		Repos: &Repositories{Error: "GitHub answered HTTP 500"}})
	sameKeys(t, failed, "kind", "result", "expires_at", "account", "installation", "repository_selection", "repositories", "repositories_error")
	if failed["repository_selection"] != nil || failed["repositories"] != nil || failed["repositories_error"] != "GitHub answered HTTP 500" {
		t.Fatal(failed)
	}

	pending := encode(t, Credential{Kind: GitHubToken, Result: Pending, Reason: "GitHub unreachable: DELETE /installation/token", Account: "acme"})
	sameKeys(t, pending, "kind", "result", "expires_at", "account", "reason")
	if encode(t, Credential{Kind: GitHubToken, Result: Pending, Account: "acme"})["reason"] != "unknown" {
		t.Fatal("a pending entry always has a reason")
	}
	if _, ok := encode(t, Credential{Kind: GitHubToken, Result: Revoked, Reason: "x", Account: "acme"})["reason"]; ok {
		t.Fatal("reason is only for pending")
	}
}

func TestLineJSON(t *testing.T) {
	at := time.Date(2026, 10, 17, 12, 0, 0, 5, time.FixedZone("x", 3600))
	start := encode(t, Line{Time: at, Command: "start", Event: "start", Container: ctr,
		Start: &StartFacts{Image: "ensemble-worker:" + "ab", Mounts: []Mount{{Type: "tmpfs", Target: "/run/ensemble/secrets", Mode: "rw"}}}})
	sameKeys(t, start, "time", "command", "event", "session", "generation", "container", "image", "image_digest", "mounts", "credentials", "error")
	if start["time"] != "2026-10-17T11:00:00.000000005Z" || start["session"] != "worker_1" ||
		start["generation"] != "0123456789abcdef0123456789abcdef" || start["image_digest"] != nil || start["error"] != nil {
		t.Fatal(start)
	}
	mount := start["mounts"].([]any)[0].(map[string]any)
	sameKeys(t, mount, "type", "target", "mode")
	if list, ok := start["credentials"].([]any); !ok || len(list) != 0 {
		t.Fatal("credentials is never null")
	}

	unparsed := encode(t, Line{Time: at, Command: "start", Event: "start", Error: "manifest_rejected"})
	for _, k := range []string{"session", "generation", "container", "image", "image_digest", "mounts"} {
		if v, ok := unparsed[k]; !ok || v != nil {
			t.Fatalf("%s: %v", k, unparsed)
		}
	}
	if unparsed["error"] != "manifest_rejected" {
		t.Fatal(unparsed)
	}

	closed := encode(t, Line{Time: at, Command: "close", Event: "close", Container: ctr})
	sameKeys(t, closed, "time", "command", "event", "session", "generation", "container", "credentials", "error")
}

func TestSplit(t *testing.T) {
	for _, c := range []struct{ in, session, generation string }{
		{ctr, "worker_1", "0123456789abcdef0123456789abcdef"},
		{"ensemble-a-b-c-0123456789abcdef0123456789abcdef", "a-b-c", "0123456789abcdef0123456789abcdef"},
		{"ensemble--0123456789abcdef0123456789abcdef", "", ""},
		{"ensemble-x-0123456789ABCDEF0123456789abcdef", "", ""},
		{"other-x-0123456789abcdef0123456789abcdef", "", ""},
		{"", "", ""},
	} {
		s, g := Split(c.in)
		if s != c.session || g != c.generation {
			t.Errorf("Split(%q) = %q, %q", c.in, s, g)
		}
	}
}
