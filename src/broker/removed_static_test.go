package main

import (
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"ensemble/broker/internal/protocol"
)

func TestRemovedLoginDatesRejectBeforeKeychain(t *testing.T) {
	for _, client := range []string{"claude", "opencode", "env"} {
		for _, flag := range []string{"--expires", "--issued", "--fresh-token"} {
			t.Run(client+flag, func(t *testing.T) {
				accessed := false
				a := app{
					keychain: func(bool) (keys, error) { accessed = true; return keys{}, nil },
				}
				args := []string{"login", client, "work", flag}
				if flag != "--fresh-token" {
					args = append(args, "2099-01-01")
				}
				code, out, errOut := invoke(a, args...)
				if code != protocol.Exit(protocol.Usage) || out != "" || !strings.Contains(errOut, `"code":"usage"`) || accessed {
					t.Fatalf("code=%d out=%q stderr=%q keychain=%v", code, out, errOut, accessed)
				}
			})
		}
	}
}

func TestCommandsIgnoreOldStaticFiles(t *testing.T) {
	for _, state := range []string{"present", "missing", "corrupt"} {
		t.Run(state, func(t *testing.T) {
			home := t.TempDir()
			content := `{"secrets":{"ensemble-claude/work":{"version":2,"expires_at":"1970-01-01T00:00:00Z"}}}`
			if state == "corrupt" {
				content = `{"secrets":`
			}
			if state != "missing" {
				staticFile(t, home, content)
			}
			var calls []call
			a := recording(&calls)
			a.legacy = func() ([]string, error) { return legacyState(legacyKeys{}) }
			logins := 0
			a.login = func(keys, string, io.Writer) error { logins++; return nil }
			a.loginOpencode = func(keys, string, string, io.Writer) error { logins++; return nil }
			a.loginEnv = func(keys, string, io.Writer) error { logins++; return nil }
			for _, args := range [][]string{
				{"login", "claude", "work"}, {"login", "opencode", "lan"}, {"login", "env", "build-key"},
				{"status"}, {"start", "--manifest", "/tmp/m.json"},
				{"maintain", "--container", "ensemble-w1-" + strings.Repeat("a", 32), "--manifest", "/tmp/m.json"},
			} {
				if code, _, errOut := invoke(a, args...); code != 0 {
					t.Fatalf("%q: code=%d stderr=%s", args, code, errOut)
				}
			}
			if logins != 3 || len(calls) != 3 {
				t.Fatalf("logins=%d calls=%v", logins, calls)
			}
			path := filepath.Join(home, ".ensemble/docker/static-secrets.json")
			data, err := os.ReadFile(path)
			if state == "missing" {
				if !os.IsNotExist(err) {
					t.Fatalf("old file created: %q %v", data, err)
				}
			} else if err != nil || string(data) != content {
				t.Fatalf("old file changed: %q %v", data, err)
			}
			if _, err := os.Lstat(filepath.Join(home, ".ensemble/docker/static-secrets.lock")); !os.IsNotExist(err) {
				t.Fatalf("static lock created: %v", err)
			}
		})
	}
}
