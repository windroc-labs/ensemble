package launcher

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"slices"
	"testing"
)

func TestStartAndMaintainIgnoreOldStaticFiles(t *testing.T) {
	for _, client := range []string{"claude", "opencode"} {
		for _, state := range []string{"present", "missing", "corrupt"} {
			t.Run(client+"/"+state, func(t *testing.T) {
				creds, keys, env := &fakeCreds{}, &fakeKeys{}, &fakeEnv{}
				h, _ := startHost(&world{labels: goodLabels()}, creds)
				h.Home, h.Opencode, h.Env = t.TempDir(), keys, env
				parent := filepath.Join(h.Home, ".ensemble/docker-state/worker_1")
				if err := os.MkdirAll(parent, 0o700); err != nil {
					t.Fatal(err)
				}
				content := `{"secrets":{"ensemble-broker-claude/work":{"version":3,"expires_at":"1970-01-01T00:00:00Z"}},"injected":{},"delivered":[]}`
				if state == "corrupt" {
					content = `{"secrets":`
				}
				files := []string{filepath.Join(h.Home, ".ensemble/docker/static-secrets.json"), filepath.Join(parent, "0123456789abcdef0123456789abcdef.static.json")}
				if state != "missing" {
					for _, path := range files {
						if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
							t.Fatal(err)
						}
						if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
							t.Fatal(err)
						}
					}
				}
				path := editedManifest(t, func(m map[string]any) {
					m["client"] = client
					withSecrets([]any{secret("ensemble-broker-env/build-key", "build-key")})(m)
					if client == "claude" {
						m["claude_dir"] = filepath.Join(parent, "claude")
					} else {
						delete(m, "claude_dir")
						delete(m, "claude_account")
						m["opencode_dir"], m["opencode_providers"] = filepath.Join(parent, "opencode"), []string{"lan"}
					}
				})
				if _, err := h.Start(context.Background(), path); err != nil {
					t.Fatal(err)
				}
				if client == "claude" && !slices.Equal(creds.calls, []string{"ready work", "inject work"}) {
					t.Fatalf("Claude references: %v", creds.calls)
				}
				if client == "opencode" && !slices.Equal(keys.calls, []string{"ready lan", "inject lan"}) {
					t.Fatalf("OpenCode references: %v", keys.calls)
				}
				if !slices.Equal(env.calls, []string{"ready ensemble-broker-env/build-key", "inject ensemble-broker-env/build-key build-key"}) {
					t.Fatalf("env references: %v", env.calls)
				}
				result, err := h.Maintain(context.Background(), container, path, false)
				if err != nil {
					t.Fatal(err)
				}
				data, _ := json.Marshal(result)
				want := `{"container":"` + container + `","next_check":null,"deadline":null,"results":[]}`
				if string(data) != want {
					t.Errorf("maintenance: %s", data)
				}
				for _, path := range files {
					data, err := os.ReadFile(path)
					if state == "missing" {
						if !os.IsNotExist(err) {
							t.Fatalf("created %s: %q %v", path, data, err)
						}
					} else if err != nil || string(data) != content {
						t.Fatalf("changed %s: %q %v", path, data, err)
					}
				}
				if _, err := os.Lstat(filepath.Join(h.Home, ".ensemble/docker/static-secrets.lock")); !os.IsNotExist(err) {
					t.Fatalf("created static lock: %v", err)
				}
			})
		}
	}
}
