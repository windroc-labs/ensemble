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

// refreshCodexHost is a running Codex worker_1 container (with GitHub tokens when github) and its state directory.
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
	return h, writeManifest(t, raw), parent, tokens, creds
}

func TestMaintainRecordFailureIsEachProvidersResultAndRecovers(t *testing.T) {
	h, manifest, tokens, creds := maintainHost(t, true, nil)
	record := filepath.Join(h.Home, ".ensemble/docker-state/worker_1", tokensGeneration+".tokens.json")
	if err := os.Mkdir(record, 0o700); err != nil {
		t.Fatal(err)
	}
	got, err := h.Maintain(context.Background(), container, manifest, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	for _, kind := range []string{"codex", "github"} {
		if e := results(got)[kind].Error; e == nil || e.Code != protocol.InternalError || e.Message != "Recording "+tokensGeneration+".tokens.json failed." {
			t.Fatalf("%s: %+v", kind, e)
		}
	}
	if creds.injected != 1 || !slices.Contains(tokens.calls, "refresh ensemble-bot "+container) || got.Deadline != nil {
		t.Fatalf("%+v %d %q", got, creds.injected, tokens.calls)
	}
	if err := os.Remove(record); err != nil {
		t.Fatal(err)
	}
	got, err = h.Maintain(context.Background(), container, manifest, nil, false)
	if err != nil || results(got)["codex"].Error != nil || results(got)["github"].Error != nil || got.Deadline == nil {
		t.Fatalf("%+v %v", got, err)
	}
	files, _ := os.ReadDir(filepath.Dir(record))
	var names []string
	for _, f := range files {
		names = append(names, f.Name())
	}
	if !slices.Equal(names, []string{tokensGeneration + ".tokens.json", tokensGeneration + ".tokens.lock"}) {
		t.Fatalf("left staging files: %q", names)
	}
}

func TestMaintainRejectsUnsafeStateBeforeInjection(t *testing.T) {
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
			if _, err := h.Maintain(context.Background(), c, manifest, nil, false); err == nil || creds.injected != 0 || len(tokens.calls) != 0 {
				t.Fatalf("%v %v %v", err, creds.injected, tokens.calls)
			}
		})
	}
}

func TestTokenRecordReplacesLinkWithoutFollowingIt(t *testing.T) {
	h, manifest, _, _ := maintainHost(t, false, nil)
	record := filepath.Join(h.Home, ".ensemble/docker-state/worker_1", tokensGeneration+".tokens.json")
	unrelated := filepath.Join(t.TempDir(), "unrelated.json")
	if err := os.WriteFile(unrelated, []byte("untouched"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(unrelated, record); err != nil {
		t.Fatal(err)
	}
	if got, err := h.Maintain(context.Background(), container, manifest, nil, false); err != nil || results(got)["codex"].Error != nil {
		t.Fatalf("%+v %v", got, err)
	}
	raw, err := os.ReadFile(unrelated)
	if err != nil || string(raw) != "untouched" {
		t.Fatalf("%q %v", raw, err)
	}
	info, err := os.Lstat(record)
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm() != 0o600 {
		t.Fatalf("%v %v", info, err)
	}
}

func TestMaintainKeepsCodexErrorAndRenewsGitHub(t *testing.T) {
	h, manifest, tokens, creds := maintainHost(t, true, nil)
	failed := protocol.New(protocol.RefreshFailed, "Writing Codex credentials into the container failed")
	failed.Cause = "bootstrap: cannot write managed Codex credentials: No space left on device"
	creds.err = failed
	got, err := h.Maintain(context.Background(), container, manifest, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	if e := results(got)["codex"].Error; e == nil || e.Code != protocol.RefreshFailed || e.Message != failed.Message || e.Cause != failed.Cause {
		t.Fatalf("%+v", e)
	}
	if !slices.Equal(tokens.calls, []string{"ready ensemble-bot", "refresh ensemble-bot " + container}) || results(got)["github"].Error != nil {
		t.Fatalf("GitHub not renewed: %v", tokens.calls)
	}
}

func TestMaintainReportsBothFailures(t *testing.T) {
	for _, step := range []string{"ready", "refresh"} {
		t.Run(step, func(t *testing.T) {
			h, manifest, tokens, creds := maintainHost(t, true, nil)
			creds.err = protocol.New(protocol.LoginRequired, "Codex login unavailable; run ensemble-broker login codex")
			github := protocol.New(protocol.GitHubFailed, "GitHub request failed: HTTP 502")
			if step == "ready" {
				tokens.ready = github
			} else {
				tokens.refresh = github
			}
			got, err := h.Maintain(context.Background(), container, manifest, nil, false)
			r := results(got)
			if err != nil || r["codex"].Error == nil || r["codex"].Error.Code != protocol.LoginRequired ||
				r["github"].Error == nil || r["github"].Error.Code != protocol.GitHubFailed || r["github"].Error.Message != github.Message {
				t.Fatalf("%+v %v", got, err)
			}
		})
	}
}
