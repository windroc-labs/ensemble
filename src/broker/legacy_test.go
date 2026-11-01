package main

import (
	"errors"
	"io"
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
			found, err := legacyState(legacyKeys{services: services})
			if err != nil || !slices.Equal(found, []string{old + " x"}) {
				t.Fatalf("%q %v", found, err)
			}
		})
	}
	for name, tc := range map[string]struct {
		services map[string][]string
		want     []string
	}{
		"none":        {renamed, nil},
		"env service": {map[string][]string{"ensemble-env/db": {"db"}}, []string{"ensemble-env/db db"}},
		"items":       {map[string][]string{"ensemble-tokens": {"ensemble-ws-a", "ensemble-ws-b"}}, []string{"ensemble-tokens ensemble-ws-a", "ensemble-tokens ensemble-ws-b"}},
		"mixed services": {
			map[string][]string{"ensemble-claude": {"default"}, "ensemble-env/db": {"db"}},
			[]string{"ensemble-claude default", "ensemble-env/db db"},
		},
		// mini-op's v0.4.1 install (U1b) has eight old-name Keychain items.
		"U1b": {
			map[string][]string{
				"ensemble-claude": {"default"}, "ensemble-bot": {"123456"}, "ensemble-github-users": {"ensemble-bot"},
				"ensemble-tokens": {"ensemble-ws-w1"}, "ensemble-revocations": {"pending"}, "ensemble-codex": {"auth"},
				"ensemble-codex-grants": {"ensemble-ws-w1"}, "ensemble-linear": {"harness"},
			},
			[]string{"ensemble-bot 123456", "ensemble-claude default", "ensemble-codex auth", "ensemble-codex-grants ensemble-ws-w1",
				"ensemble-github-users ensemble-bot", "ensemble-linear harness", "ensemble-revocations pending", "ensemble-tokens ensemble-ws-w1"},
		},
		"bot account":    {map[string][]string{"ensemble-broker-github-users": {"ensemble-bot"}}, []string{"ensemble-broker-github-users ensemble-bot"}},
		"orchestrator":   {map[string][]string{"linear-app-harness-secret": {"ensemble-claude"}}, nil},
		"empty keychain": {nil, nil},
	} {
		t.Run(name, func(t *testing.T) {
			found, err := legacyState(legacyKeys{services: tc.services})
			if err != nil || !slices.Equal(found, tc.want) {
				t.Fatalf("%q %v, want %q", found, err, tc.want)
			}
		})
	}
}

func TestLegacyStateErrors(t *testing.T) {
	if _, err := legacyState(legacyKeys{err: errors.New("keychain status -25291")}); err == nil || !strings.Contains(err.Error(), "-25291") {
		t.Fatalf("listing error: %v", err)
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
	refusal := `old-name broker items found (4: "ensemble-claude", "ensemble-tokens", "ensemble-env/db"): run the maintainer's one-time rename step, which renames them to ensemble-broker-*, then rerun`
	for _, args := range [][]string{{"retrust"}, {"login", "claude", "work"}, {"linear-maintain"}} {
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
