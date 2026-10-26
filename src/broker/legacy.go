package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"slices"
	"strings"
)

// Migration code (spec D13): remove legacyState, legacyServices and legacyRefPrefixes at the next broker protocol bump
// after 30. Protocol 30 renamed every broker keychain service and static secret reference to start with
// ensemble-broker-; the maintainer's one-time step renames existing items and keys, and until it has, every command
// refuses to run rather than read or write under either name.
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

// legacyError refuses a command while the one-time rename is incomplete, naming the first old names found; nil when
// it is complete.
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
