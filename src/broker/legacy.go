package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"slices"
	"strings"

	"ensemble/broker/internal/broker"
)

// The old-name guard (round 8): protocol 30 renamed every broker keychain service and static secret reference to start
// with ensemble-broker-, and the maintainer's one-time step renames existing items and keys. While any old name remains,
// every command but version refuses to run rather than read or write under either name. It reads no old-name item and
// maps no name; remove it at the next broker protocol bump after 30 (spec D13).
var (
	legacyServices = []string{"ensemble-claude", "ensemble-bot", "ensemble-github-users", "ensemble-tokens",
		"ensemble-revocations", "ensemble-codex", "ensemble-codex-grants", "ensemble-opencode", "ensemble-linear",
		"ensemble-linear-grants"}
	legacyRefPrefixes = []string{"ensemble-claude/", "ensemble-opencode/", "ensemble-env/"}
)

// legacyKeychain lists the login keychain's items by attributes only, never reading a value or showing a dialog.
type legacyKeychain interface {
	Services(prefix string) ([]string, error)
	Accounts(service string) ([]string, error)
}

// legacyState is what the one-time rename left under an old name: each old service (one of legacyServices, or an
// ensemble-env/<name> one), the bot user item's old account ensemble-bot, and each static-secrets.json key with an
// old prefix; none when the rename is complete.
func legacyState(home string, k legacyKeychain) ([]string, error) {
	services, err := k.Services("ensemble-")
	if err != nil {
		return nil, err
	}
	var found []string
	for _, s := range services {
		if slices.Contains(legacyServices, s) || strings.HasPrefix(s, "ensemble-env/") {
			found = append(found, s)
		}
	}
	accounts, err := k.Accounts("ensemble-broker-github-users")
	if err != nil {
		return nil, err
	}
	if slices.Contains(accounts, "ensemble-bot") {
		found = append(found, "ensemble-broker-github-users ensemble-bot")
	}
	path := staticStore(home).Path
	data, err := os.ReadFile(path)
	if errors.Is(err, fs.ErrNotExist) {
		return found, nil
	}
	var static struct {
		Secrets map[string]json.RawMessage `json:"secrets"`
	}
	if err == nil {
		err = json.Unmarshal(data, &static)
	}
	if err != nil {
		return nil, fmt.Errorf("reading %s failed: %w", path, err)
	}
	var refs []string
	for ref := range static.Secrets {
		if slices.ContainsFunc(legacyRefPrefixes, func(p string) bool { return strings.HasPrefix(ref, p) }) {
			refs = append(refs, ref)
		}
	}
	slices.Sort(refs)
	return append(found, refs...), nil
}

// systemLegacy is legacyState of the account's home and login keychain.
func systemLegacy() ([]string, error) {
	home, err := accountHome()
	if err != nil {
		return nil, err
	}
	var k legacyKeychain = keys{Home: home}
	// Not the installed broker (credential access refuses it anyway), or root's install-codex without a login
	// keychain: only the static secrets' keys to check.
	if _, err := os.Lstat(filepath.Join(home, "Library/Keychains/login.keychain-db")); err != nil || broker.CheckInstalled() != nil {
		k = noKeychain{}
	}
	return legacyState(home, k)
}

// noKeychain is a keychain without items.
type noKeychain struct{}

func (noKeychain) Services(string) ([]string, error) { return nil, nil }
func (noKeychain) Accounts(string) ([]string, error) { return nil, nil }

// legacyError refuses a command while the one-time rename is incomplete, naming the first old names found; nil when
// it is complete. scripts/install.sh's broker_logins looks for "old-name broker items found" in status's error.
func (a app) legacyError() error {
	found, err := a.legacy()
	if err != nil {
		return fmt.Errorf("cannot check for old-name broker items: %w", err)
	}
	if len(found) == 0 {
		return nil
	}
	quoted := make([]string, 0, 3)
	for _, f := range found[:min(3, len(found))] {
		quoted = append(quoted, fmt.Sprintf("%q", f))
	}
	return fmt.Errorf("old-name broker items found (%d: %s): run the maintainer's one-time rename step (docs/upgrading.md), then rerun", len(found), strings.Join(quoted, ", "))
}
