package main

import (
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"

	"ensemble/broker/internal/broker"
)

// The old-name guard: protocol 30 renamed broker keychain services to start with ensemble-broker-. While any old name remains,
// every command but version refuses to run rather than read or write under either name. It reads no old-name item and
// maps no name; remove it in a later change, once no protocol-30 install is left.
var (
	legacyServices = []string{"ensemble-claude", "ensemble-bot", "ensemble-github-users", "ensemble-tokens",
		"ensemble-revocations", "ensemble-codex", "ensemble-codex-grants", "ensemble-opencode", "ensemble-linear",
		"ensemble-linear-grants"}
)

// legacyKeychain lists the login keychain's items by attributes only, never reading a value or showing a dialog.
type legacyKeychain interface {
	Services(prefix string) ([]string, error)
	Accounts(service string) ([]string, error)
}

// legacyState lists items that still use an old Keychain name.
func legacyState(k legacyKeychain) ([]string, error) {
	services, err := k.Services("ensemble-")
	if err != nil {
		return nil, err
	}
	var found []string
	for _, s := range services {
		if !slices.Contains(legacyServices, s) && !strings.HasPrefix(s, "ensemble-env/") {
			continue
		}
		accounts, err := k.Accounts(s)
		if err != nil {
			return nil, err
		}
		if len(accounts) == 0 {
			found = append(found, s)
		}
		for _, a := range accounts {
			found = append(found, s+" "+a)
		}
	}
	accounts, err := k.Accounts("ensemble-broker-github-users")
	if err != nil {
		return nil, err
	}
	if slices.Contains(accounts, "ensemble-bot") {
		found = append(found, "ensemble-broker-github-users ensemble-bot")
	}

	return found, nil
}

// systemLegacy is legacyState of the account's home and login keychain.
func systemLegacy() ([]string, error) {
	home, err := accountHome()
	if err != nil {
		return nil, err
	}
	var k legacyKeychain = keys{Home: home}
	// A non-installed broker or an account without a login keychain has no items to inspect.
	if _, err := os.Lstat(filepath.Join(home, "Library/Keychains/login.keychain-db")); err != nil || broker.CheckInstalled() != nil {
		k = noKeychain{}
	}
	return legacyState(k)
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
	return fmt.Errorf("old-name broker items found (%d: %s): run the maintainer's one-time rename step, which renames them to ensemble-broker-*, then rerun", len(found), strings.Join(quoted, ", "))
}
