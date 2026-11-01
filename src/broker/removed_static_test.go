package main

import (
	"context"
	"errors"
	"io"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"ensemble/broker/internal/broker"
	"ensemble/broker/internal/launcher"
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
			files := []string{
				filepath.Join(home, ".ensemble/docker/static-secrets.json"),
				filepath.Join(home, ".ensemble/docker/static-secrets.lock"),
				filepath.Join(home, ".ensemble/docker-state/worker_1/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.static.json"),
			}
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
			var calls []call
			a := recording(&calls)
			a.keychain = func(bool) (keys, error) { return keys{Home: home}, nil }
			a.legacy = func() ([]string, error) { return legacyState(legacyKeys{}) }
			stored := memoryKeychain{}
			checkHome := func(k keys) {
				if k.Home != home {
					t.Fatalf("wrong command home: %q", k.Home)
				}
			}
			read := func(value string) func() ([]byte, error) {
				return func() ([]byte, error) { return []byte(value), nil }
			}
			password := func() ([]byte, error) { t.Fatal("trusted fake requested password"); return nil, nil }
			a.login = func(k keys, account string, out io.Writer) error {
				checkHome(k)
				return broker.Login(account, read("claude-fixture"), password, stored, io.Discard, out)
			}
			a.loginOpencode = func(k keys, provider, _ string, out io.Writer) error {
				checkHome(k)
				return broker.LoginOpencode(provider, read("opencode-fixture"), password, stored, out)
			}
			a.loginEnv = func(k keys, name string, out io.Writer) error {
				checkHome(k)
				return broker.LoginEnv(name, read("env-fixture"), password, stored, out)
			}
			a.status = func(_ context.Context, k keys) (any, error) {
				checkHome(k)
				accounts, err := stored.Accounts("ensemble-broker-claude")
				return launcher.Status(accounts, broker.Credentials{Keychain: stored}.Usable), err
			}
			for _, tc := range []struct {
				args []string
				want string
			}{
				{[]string{"login", "claude", "work"}, "Claude account work setup-token saved; keychain trusts only the broker\n"},
				{[]string{"login", "opencode", "lan"}, "OpenCode provider lan API key saved; keychain trusts only the broker\n"},
				{[]string{"login", "env", "build-key"}, "Environment secret ensemble-broker-env/build-key saved; keychain trusts only the broker\n"},
				{[]string{"status"}, "{\"claude\":{\"work\":true}}\n"},
				{[]string{"start", "--manifest", "/tmp/m.json"}, "{\"ok\":\"start\"}\n"},
				{[]string{"maintain", "--container", "ensemble-w1-" + strings.Repeat("a", 32), "--manifest", "/tmp/m.json"}, "{\"ok\":\"maintain\"}\n"},
			} {
				if code, out, errOut := invoke(a, tc.args...); code != 0 || out != tc.want || errOut != "" {
					t.Fatalf("%q: code=%d stdout=%q stderr=%q", tc.args, code, out, errOut)
				}
			}
			if len(calls) != 2 || len(stored) != 3 || string(stored["ensemble-broker-claude/work"]) != "claude-fixture" ||
				string(stored["ensemble-broker-opencode/lan"]) != "opencode-fixture" ||
				string(stored["ensemble-broker-env/build-key/build-key"]) != "env-fixture" {
				t.Fatalf("wrong key references or command calls: %v", calls)
			}
			for _, path := range files {
				data, err := os.ReadFile(path)
				if state == "missing" {
					if !os.IsNotExist(err) {
						t.Fatalf("old file created: %s %v", path, err)
					}
				} else if err != nil || string(data) != content {
					t.Fatalf("old file changed: %s %v", path, err)
				}
			}
		})
	}
}

type memoryKeychain map[string][]byte

func (k memoryKeychain) Secret(service, account string) ([]byte, error) {
	value, ok := k[service+"/"+account]
	if !ok {
		return nil, broker.ErrNotFound
	}
	return slices.Clone(value), nil
}
func (k memoryKeychain) PutSecret(service, account string, value []byte) error {
	k[service+"/"+account] = slices.Clone(value)
	return nil
}
func (k memoryKeychain) DeleteSecret(service, account string) error {
	delete(k, service+"/"+account)
	return nil
}
func (k memoryKeychain) Get(account string) ([]byte, error) {
	return k.Secret("ensemble-broker-claude", account)
}
func (k memoryKeychain) Put(account string, value []byte) error {
	return k.PutSecret("ensemble-broker-claude", account, value)
}
func (memoryKeychain) GetWithDialog(string) ([]byte, error) {
	return nil, errors.New("unexpected dialog")
}
func (k memoryKeychain) AppAccess(service, account string) (broker.AppAccess, error) {
	if _, ok := k[service+"/"+account]; !ok {
		return broker.AppAccess{}, broker.ErrNotFound
	}
	return broker.AppAccess{Broker: "fixture", Partition: "fixture", Entries: []broker.ACLEntry{
		{Authorizations: []string{"decrypt"}, Apps: []string{"fixture"}},
		{Authorizations: []string{"partition_id"}, Partitions: []string{"fixture"}},
	}}, nil
}
func (memoryKeychain) TrustApps(string, string, []byte) error {
	return errors.New("unexpected trust change")
}
func (memoryKeychain) TrustAppsByDialog(string, string) error {
	return errors.New("unexpected trust dialog")
}
func (memoryKeychain) TrustPartition(string, string, []byte) error {
	return errors.New("unexpected partition change")
}
func (k memoryKeychain) Accounts(service string) ([]string, error) {
	var accounts []string
	for item := range k {
		if name, ok := strings.CutPrefix(item, service+"/"); ok {
			accounts = append(accounts, name)
		}
	}
	slices.Sort(accounts)
	return accounts, nil
}
