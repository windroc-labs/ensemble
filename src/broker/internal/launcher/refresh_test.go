package launcher

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"testing"

	"ensemble/broker/internal/protocol"
)

func refreshCodexHost(t *testing.T, github bool) (Host, string, string, *fakeTokens, *fakeCodexCreds) {
	t.Helper()
	h, _ := startHost(&world{labels: goodLabels()}, &fakeCreds{})
	h.Home = t.TempDir()
	tokens, creds := &fakeTokens{}, &fakeCodexCreds{}
	h.GitHub, h.Codex = tokens, creds
	raw, err := os.ReadFile(codexManifest(t))
	if err != nil {
		t.Fatal(err)
	}
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		t.Fatal(err)
	}
	parent := filepath.Join(h.Home, ".ensemble/docker-state", "worker_1")
	m["codex_dir"] = filepath.Join(parent, "codex")
	if github {
		m["github"] = map[string]any{"app": "ensemble-bot", "permissions": fixtureSet}
	}
	if err := os.MkdirAll(parent, 0o700); err != nil {
		t.Fatal(err)
	}
	raw, err = json.Marshal(m)
	if err != nil {
		t.Fatal(err)
	}
	return h, writeManifest(t, raw), filepath.Join(parent, "0123456789abcdef0123456789abcdef.credential.json"), tokens, creds
}

func TestRefreshCodexWithoutGitHubPublishesDeadline(t *testing.T) {
	h, manifest, deadline, tokens, creds := refreshCodexHost(t, false)
	got, err := h.Refresh(context.Background(), container, manifest)
	if err != nil {
		t.Fatal(err)
	}
	if creds.injected != 1 || len(tokens.calls) != 0 || got.GitHub == nil || len(got.GitHub) != 0 {
		t.Fatalf("%+v %v %v", got, tokens.calls, creds.injected)
	}
	raw, err := os.ReadFile(deadline)
	if err != nil || string(raw) != `"2026-10-18T12:00:00Z"` {
		t.Fatalf("deadline %q %v", raw, err)
	}
	encoded, _ := json.Marshal(got)
	var fields map[string]any
	_ = json.Unmarshal(encoded, &fields)
	if fields["credential_expires_at"] != "2026-10-18T12:00:00Z" {
		t.Fatalf("%s", encoded)
	}
}

func TestRefreshRepairsDeadlineBeforeGitHubFailure(t *testing.T) {
	h, manifest, deadline, tokens, creds := refreshCodexHost(t, true)
	if err := os.Mkdir(deadline, 0o700); err != nil {
		t.Fatal(err)
	}
	_, err := h.Refresh(context.Background(), container, manifest)
	if code(t, err) != protocol.RefreshFailed || creds.injected != 1 || !slices.Contains(tokens.calls, "refresh ensemble-bot "+container) {
		t.Fatalf("%v %v %v", err, creds.injected, tokens.calls)
	}
	tokens.calls = nil
	if err := os.Remove(deadline); err != nil {
		t.Fatal(err)
	}
	tokens.ready = protocol.New(protocol.GitHubFailed, "unavailable")
	_, err = h.Refresh(context.Background(), container, manifest)
	if code(t, err) != protocol.GitHubFailed || creds.injected != 2 {
		t.Fatalf("%v %v", err, creds.injected)
	}
	raw, err := os.ReadFile(deadline)
	if err != nil || string(raw) != `"2026-10-18T12:00:00Z"` {
		t.Fatalf("deadline %q %v", raw, err)
	}
	tokens.ready = nil
	if _, err := h.Refresh(context.Background(), container, manifest); err != nil {
		t.Fatal(err)
	}
	raw, err = os.ReadFile(deadline)
	if err != nil || string(raw) != `"2026-10-18T12:00:00Z"` {
		t.Fatalf("lost-response retry %q %v", raw, err)
	}
	files, _ := os.ReadDir(filepath.Dir(deadline))
	if len(files) != 1 {
		t.Fatalf("left staging files: %v", files)
	}
}

func TestRefreshRejectsUnsafeStateBeforeInjection(t *testing.T) {
	for _, kind := range []string{"container", "directory", "symlink"} {
		t.Run(kind, func(t *testing.T) {
			h, manifest, _, tokens, creds := refreshCodexHost(t, false)
			c := container
			switch kind {
			case "container":
				c = "ensemble-other-0123456789abcdef0123456789abcdef"
			case "directory":
				h.Home = "/arbitrary"
			case "symlink":
				h.Safe = func(string) error { return errors.New("link") }
			}
			if _, err := h.Refresh(context.Background(), c, manifest); err == nil || creds.injected != 0 || len(tokens.calls) != 0 {
				t.Fatalf("%v %v %v", err, creds.injected, tokens.calls)
			}
		})
	}
}

func TestRefreshDeadlineReplacesLinkWithoutFollowingIt(t *testing.T) {
	h, manifest, deadline, _, _ := refreshCodexHost(t, false)
	unrelated := filepath.Join(t.TempDir(), "unrelated.json")
	if err := os.WriteFile(unrelated, []byte("untouched"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(unrelated, deadline); err != nil {
		t.Fatal(err)
	}
	if _, err := h.Refresh(context.Background(), container, manifest); err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(unrelated)
	if err != nil || string(raw) != "untouched" {
		t.Fatalf("%q %v", raw, err)
	}
	info, err := os.Lstat(deadline)
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm() != 0o600 {
		t.Fatalf("%v %v", info, err)
	}
}

func TestRefreshRenewsGitHubWhenCodexFails(t *testing.T) {
	h, manifest, _, tokens, creds := refreshCodexHost(t, true)
	failed := protocol.New(protocol.RefreshFailed, "Writing Codex credentials into the container failed")
	failed.Cause = "bootstrap: cannot write managed Codex credentials: No space left on device"
	creds.err = failed
	_, err := h.Refresh(context.Background(), container, manifest)
	var e *protocol.Error
	if !errors.As(err, &e) || e.Code != protocol.RefreshFailed || e.Message != failed.Message || e.Cause != failed.Cause {
		t.Fatalf("%#v", err)
	}
	if !slices.Equal(tokens.calls, []string{"ready ensemble-bot", "refresh ensemble-bot " + container}) {
		t.Fatalf("GitHub not renewed: %v", tokens.calls)
	}
}

func TestRefreshReportsBothFailures(t *testing.T) {
	for _, step := range []string{"ready", "refresh"} {
		t.Run(step, func(t *testing.T) {
			h, manifest, _, tokens, creds := refreshCodexHost(t, true)
			creds.err = protocol.New(protocol.LoginRequired, "Codex login unavailable; run ensemble-broker login codex")
			github := protocol.New(protocol.GitHubFailed, "GitHub request failed: HTTP 502")
			if step == "ready" {
				tokens.ready = github
			} else {
				tokens.refresh = github
			}
			_, err := h.Refresh(context.Background(), container, manifest)
			var e *protocol.Error
			want := "Codex login unavailable; run ensemble-broker login codex; GitHub renewal also failed: GitHub request failed: HTTP 502"
			if !errors.As(err, &e) || e.Code != protocol.LoginRequired || e.Message != want {
				t.Fatalf("%#v", err)
			}
		})
	}
}
