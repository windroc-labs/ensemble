package main

import (
	"errors"
	"io"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"ensemble/broker/internal/protocol"
)

// legacyKeys is a keychain of these services, each with these accounts.
type legacyKeys struct {
	services map[string][]string
	err      error
}

func (k legacyKeys) Services(prefix string) ([]string, error) {
	var found []string
	for s := range k.services {
		if strings.HasPrefix(s, prefix) {
			found = append(found, s)
		}
	}
	slices.Sort(found)
	return found, k.err
}

func (k legacyKeys) Accounts(service string) ([]string, error) { return k.services[service], k.err }

func staticFile(t *testing.T, home, content string) {
	t.Helper()
	dir := filepath.Join(home, ".ensemble/docker")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "static-secrets.json"), []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
}

func TestLegacyState(t *testing.T) {
	renamed := map[string][]string{
		"ensemble-broker-claude":       {"default"},
		"ensemble-broker-github-users": {"ensemble-broker-bot"},
		"ensemble-broker-env/db":       {"db"},
		"linear-app-harness-secret":    {"harness"},
	}
	for _, old := range legacyServices {
		t.Run(old, func(t *testing.T) {
			services := map[string][]string{old: {"x"}}
			for s, a := range renamed {
				services[s] = a
			}
			found, err := legacyState(t.TempDir(), legacyKeys{services: services})
			if err != nil || !slices.Equal(found, []string{old}) {
				t.Fatalf("%q %v", found, err)
			}
		})
	}
	for name, tc := range map[string]struct {
		services map[string][]string
		static   string
		want     []string
	}{
		"none":            {renamed, "", nil},
		"env service":     {map[string][]string{"ensemble-env/db": {"db"}}, "", []string{"ensemble-env/db"}},
		"bot account":     {map[string][]string{"ensemble-broker-github-users": {"ensemble-bot"}}, "", []string{"ensemble-broker-github-users ensemble-bot"}},
		"orchestrator":    {map[string][]string{"linear-app-harness-secret": {"ensemble-claude"}}, "", nil},
		"static keys":     {nil, `{"secrets":{"ensemble-claude/default":{},"ensemble-broker-env/db":{},"ensemble-opencode/strata":{},"ensemble-env/db":{}}}`, []string{"ensemble-claude/default", "ensemble-env/db", "ensemble-opencode/strata"}},
		"new static keys": {nil, `{"secrets":{"ensemble-broker-claude/default":{"version":1}}}`, nil},
	} {
		t.Run(name, func(t *testing.T) {
			home := t.TempDir()
			if tc.static != "" {
				staticFile(t, home, tc.static)
			}
			found, err := legacyState(home, legacyKeys{services: tc.services})
			if err != nil || !slices.Equal(found, tc.want) {
				t.Fatalf("%q %v, want %q", found, err, tc.want)
			}
		})
	}
}

func TestLegacyStateErrors(t *testing.T) {
	if _, err := legacyState(t.TempDir(), legacyKeys{err: errors.New("keychain status -25291")}); err == nil || !strings.Contains(err.Error(), "-25291") {
		t.Fatalf("listing error: %v", err)
	}
	home := t.TempDir()
	staticFile(t, home, `{"secrets":`)
	if _, err := legacyState(home, legacyKeys{}); err == nil {
		t.Fatal("unreadable static-secrets.json accepted")
	}
}

func TestLegacyStateBlocksCommands(t *testing.T) {
	var calls []call
	a := recording(&calls)
	ran := false
	a.retrust = func(keys, io.Writer) error { ran = true; return nil }
	a.login = func(keys, string, io.Writer) error { ran = true; return nil }
	a.keychain = func(bool) (keys, error) { ran = true; return keys{}, nil }
	a.legacy = func() ([]string, error) {
		return []string{"ensemble-claude", "ensemble-tokens", "ensemble-env/db", "ensemble-claude/default"}, nil
	}
	refusal := `old-name broker items found (4: "ensemble-claude", "ensemble-tokens", "ensemble-env/db"): run the maintainer's one-time rename step (docs/upgrading.md), then rerun`
	for _, args := range [][]string{{"retrust"}, {"login", "claude"}, {"linear-maintain"}} {
		if code, out, errOut := invoke(a, args...); code != 1 || out != "" || errOut != "ensemble-broker: "+refusal+"\n" {
			t.Fatalf("%q: %d %q %q", args, code, out, errOut)
		}
	}
	code, out, errOut := invoke(a, "status")
	assertError(t, errOut, out, "status", protocol.KeychainFailed)
	if code != protocol.Exit(protocol.KeychainFailed) || !strings.Contains(errOut, "old-name broker items found (4:") {
		t.Fatalf("%d %q", code, errOut)
	}
	if ran || len(calls) != 0 {
		t.Fatalf("a command ran: %v %v", ran, calls)
	}
	if code, out, _ := invoke(a, "version"); code != 0 || out == "" {
		t.Fatalf("version refused: %d", code)
	}
	a.legacy = func() ([]string, error) { return nil, errors.New("keychain status -25291") }
	if code, _, errOut := invoke(a, "retrust"); code != 1 || errOut != "ensemble-broker: cannot check for old-name broker items: keychain status -25291\n" || ran {
		t.Fatalf("%d %q", code, errOut)
	}
}
