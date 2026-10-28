// Package broker owns the broker's keychain secrets: each Claude account's setup-token (login, access, injection into a
// container), the GitHub App private keys (login) and the environment secrets (login, injection into a container).
// It never decides how a container starts: the launcher calls it through Credentials with an exec bound to one container.
package broker

import (
	"bytes"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"reflect"
	"regexp"
	"slices"
	"strings"
	"unicode"

	"ensemble/broker/internal/credential"
	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/github"
	"ensemble/broker/internal/launcher"
	"ensemble/broker/internal/protocol"
)

const (
	InstalledPath  = "/usr/local/libexec/ensemble/ensemble-broker"
	UpgradeCommand = "sh <ensemble plugin directory>/src/broker/install.sh"
	maxToken       = 16384
)

// A Claude account's setup-token: service tokenService, account the account's name (launcher.ValidClaudeAccount).
const tokenService = "ensemble-broker-claude"

// TokenReader is start's side of the setup-token items: an item's access (ItemTrust's AppAccess of tokenService and
// the account) and Get, a read of the account's item that fails with ErrNeedsDialog rather than show a keychain dialog.
type TokenReader interface {
	AppAccess(service, account string) (AppAccess, error)
	Get(account string) ([]byte, error)
}

// Keychain is login claude's side of the setup-token items; GetWithDialog is a read macOS may ask the user to allow.
type Keychain interface {
	TokenReader
	ItemTrust
	GetWithDialog(account string) ([]byte, error)
	Put(account string, token []byte) error
}

var (
	ErrNotFound  = errors.New("keychain item not found")
	ErrAmbiguous = errors.New("more than one keychain item")
	// ErrAuthFailed is errSecAuthFailed: a wrong keychain password, or securityd refusing a change.
	ErrAuthFailed = errors.New("keychain status -25293")
	// ErrNeedsDialog is errSecInteractionNotAllowed: a no-dialog read that would have asked the user.
	ErrNeedsDialog = errors.New("keychain status -25308")
	// ErrInvalidOwnerEdit means another process owns the item's access controls.
	ErrInvalidOwnerEdit = errors.New("keychain status -25244")
	// errAccessUnread is secureItem's first access read failing, before it changed anything.
	errAccessUnread = errors.New("reading its access failed")
	// ErrNotBrokerItem is an in-place write refused because the item does not trust only the broker.
	ErrNotBrokerItem = errors.New("keychain item does not trust only the broker")
	// errNotEarlierBuild is secureGuarded's guard refusing the item's access, before it changed anything.
	errNotEarlierBuild = errors.New("it trusts another application")
)

func ownershipError(service, account string, err error) error {
	return fmt.Errorf("keychain item %s/%s belongs to another process (%w); delete this item from the login keychain in Keychain Access, then rerun install.sh (or the broker login command)", service, account, err)
}

// AppKeyService is the item of the one GitHub App's private key (account its App ID) and login github's argument;
// the App's own slug, whatever it is, comes from GitHub at login.
const AppKeyService = "ensemble-broker-bot"

// KeychainServices are every keychain service the broker reads or writes, one literal each: docs/architecture.md names
// them all (tests/guard_checks/credentials.py reads this list). Declare each service as a package-level `...Service`
// string constant, and each prefix item, an entry ending in "/" that stands for every service starting with it, as a
// `...ServicePrefix` one: TestKeychainServices pins this list to exactly those.
var KeychainServices = []string{
	"ensemble-broker-claude",
	"ensemble-broker-bot",
	"ensemble-broker-github-users",
	"ensemble-broker-codex",
	"ensemble-broker-opencode",
	"ensemble-broker-env/", // prefix item: every ensemble-broker-env/<name>
	"ensemble-broker-linear",
	"ensemble-broker-linear-grants",
}

// SecretKeychain holds broker-only items: created trusting only the broker, read without a dialog.
type SecretKeychain interface {
	Secret(service, account string) ([]byte, error)
	PutSecret(service, account string, data []byte) error
	DeleteSecret(service, account string) error // an absent item is success
}

// brokerOnlyStore holds broker-only items and changes their access: storeBrokerOnly's side.
type brokerOnlyStore interface {
	SecretKeychain
	ItemTrust
}

// trustedItem is start's controlled check of a broker-only item, without reading it: whether its access trusts only
// the broker. err is the access read's, for the caller to map.
func trustedItem(k accessReader, service, account string) (trusted bool, err error) {
	trusted, _, err = checkItem(k, service, account)
	return trusted, err
}

type accessReader interface {
	AppAccess(service, account string) (AppAccess, error)
}

// checkItem is trustedItem, and for an item not trusted, whether an earlier broker build's (earlierBuild), which
// RetrustCommand repairs.
func checkItem(k accessReader, service, account string) (trusted, earlier bool, err error) {
	access, err := k.AppAccess(service, account)
	if err != nil {
		return false, false, err
	}
	if onlyBroker(access) == nil {
		return true, false, nil
	}
	return false, earlierBuild(access), nil
}

// retrustRemedy ends a keychain error a stale item may cause.
const retrustRemedy = ": unlock the login keychain, or run " + RetrustCommand + "."

// writeOwned runs write, an in-place update of the item's data, only when the item trusts only the broker: macOS lets
// any application update an item whose encrypt entry trusts any application, without UI, so the update itself never
// refuses another's item.
func writeOwned(k accessReader, service, account string, write func() error) error {
	access, err := k.AppAccess(service, account)
	if err != nil {
		return err
	}
	if err := onlyBroker(access); err != nil {
		return fmt.Errorf("keychain item %s/%s: %w: %w", service, account, ErrNotBrokerItem, err)
	}
	return write()
}

// readSecret is start's controlled read of a broker-only item, never with a dialog; it erases what a failed read
// returned. The caller erases the value.
func readSecret(k SecretKeychain, service, account string) ([]byte, error) {
	value, err := k.Secret(service, account)
	if err != nil {
		erase(value)
		return nil, err
	}
	return value, nil
}

// storeBrokerOnly saves value in item service/account (replacing an earlier one), makes the item trust only the
// broker (secureItem, asking password when needed) and proves start's no-dialog read returns value. Errors name the
// item and end with rerun; none holds value.
func storeBrokerOnly(service, account, rerun string, value []byte, k brokerOnlyStore, password func() ([]byte, error)) error {
	item := service + " " + account
	if err := k.PutSecret(service, account, value); err != nil {
		if errors.Is(err, ErrInvalidOwnerEdit) {
			return ownershipError(service, account, err)
		}
		return fmt.Errorf("keychain item %s: saving it failed: %w; %s", item, err, rerun)
	}
	if err := secureItem(service, account, rerun+" to finish", k, password); err != nil {
		return err
	}
	saved, err := k.Secret(service, account)
	defer erase(saved)
	if err == nil && !bytes.Equal(saved, value) {
		err = errors.New("it holds other bytes")
	}
	if err != nil {
		return fmt.Errorf("keychain item %s: reading it failed: %w; %s", item, err, rerun)
	}
	return nil
}

// ItemTrust reads and changes the access of the generic-password item service and account name.
type ItemTrust interface {
	TrustApps(service, account string, password []byte) error
	TrustAppsByDialog(service, account string) error
	TrustPartition(service, account string, password []byte) error
	AppAccess(service, account string) (AppAccess, error)
}

// AppKeychain holds GitHub App private keys as generic-password items: service the App, account its App ID.
type AppKeychain interface {
	ItemTrust
	AppAccount(app string) (string, error)
	AppKey(app, account string) ([]byte, error)
}

// AppKeyStore is login github --key-file's side: it creates the App private-key item.
type AppKeyStore interface {
	AppKeychain
	PutSecret(service, account string, data []byte) error
}

// ACLEntry is one entry of a keychain item's access. Apps are the identities of the applications it trusts
// unless Any; Partitions is the partition list a partition_id entry holds.
type ACLEntry struct {
	Authorizations []string
	Any            bool
	Apps           []string
	Partitions     []string
}

// AppAccess is a private-key item's access, beside the broker's own identity and partition ID.
type AppAccess struct {
	Entries           []ACLEntry
	Broker, Partition string
}

var readAuthorizations = []string{"any", "decrypt", "export_clear", "export_wrapped"}

// onlyBroker is nil when the item trusts only the broker: appsOnlyBroker and partitionOnlyBroker.
func onlyBroker(a AppAccess) error {
	return errors.Join(appsOnlyBroker(a), partitionOnlyBroker(a))
}

// partitionOnlyBroker is nil when the item's one partition list holds only the broker's partition ID.
func partitionOnlyBroker(a AppAccess) error {
	partitionLists := 0
	for _, e := range a.Entries {
		if slices.Contains(e.Authorizations, "partition_id") {
			partitionLists++
			if !slices.Equal(e.Partitions, []string{a.Partition}) {
				return fmt.Errorf("its partition list is %q, not [%q]", e.Partitions, a.Partition)
			}
		}
	}
	if partitionLists != 1 {
		return fmt.Errorf("it has %d partition lists, not one", partitionLists)
	}
	return nil
}

// appsOnlyBroker is nil when every entry that can read or export the item trusts only the broker and one of
// them lets it decrypt.
func appsOnlyBroker(a AppAccess) error {
	decrypt := false
	for _, e := range a.Entries {
		if !slices.ContainsFunc(e.Authorizations, func(x string) bool { return slices.Contains(readAuthorizations, x) }) {
			continue
		}
		switch {
		case e.Any:
			return fmt.Errorf("its %s entry trusts any application", strings.Join(e.Authorizations, ","))
		case len(e.Apps) == 0:
			return fmt.Errorf("its %s entry trusts no application", strings.Join(e.Authorizations, ","))
		case !slices.Equal(e.Apps, []string{a.Broker}):
			return fmt.Errorf("its %s entry trusts %q", strings.Join(e.Authorizations, ","), e.Apps)
		}
		decrypt = decrypt || slices.Contains(e.Authorizations, "decrypt") || slices.Contains(e.Authorizations, "any")
	}
	if !decrypt {
		return errors.New("no entry lets the broker read it")
	}
	return nil
}

// earlierBuild is whether the item trusts only one build of the broker at InstalledPath, this one or an earlier one:
// every read or export entry trusts exactly that one application, recorded with its code requirement, and the one
// partition list holds one cdhash ID. teamid IDs are refused: the broker was only ever signed ad hoc. A permissive or
// foreign item is never one, so only such an item is re-trusted with the keychain password.
func earlierBuild(a AppAccess) bool {
	previous, partition := "", ""
	for _, entry := range a.Entries {
		if slices.ContainsFunc(entry.Authorizations, func(auth string) bool { return slices.Contains(readAuthorizations, auth) }) {
			if entry.Any || len(entry.Apps) != 1 {
				return false
			}
			app := entry.Apps[0]
			if !strings.HasPrefix(app, InstalledPath+" (") || !strings.HasSuffix(app, ")") || previous != "" && previous != app {
				return false
			}
			previous = app
		}
		if slices.Contains(entry.Authorizations, "partition_id") {
			if partition != "" || len(entry.Partitions) != 1 {
				return false
			}
			partition = entry.Partitions[0]
		}
	}
	if previous == "" || !strings.HasPrefix(partition, "cdhash:") {
		return false
	}
	a.Broker, a.Partition = previous, partition
	return onlyBroker(a) == nil
}

// identity names an application by its path and, when it records one, its code requirement, so a rebuilt
// broker at the same path is another application.
func identity(path, requirement string) string {
	if requirement == "" {
		return path
	}
	return path + " (" + requirement + ")"
}

// parseAccess reads the access text keychain_darwin.go writes: one record per line, each field lowercase hex
// so no stored string can forge a record. "self <path> <requirement> <partition ID>" once; "acl <authorization>..."
// starts an entry, and "any", "app <path> <requirement>" and "partition <ID>" add to the last one.
func parseAccess(text []byte) (AppAccess, error) {
	var a AppAccess
	self := false
	for line := range strings.Lines(string(text)) {
		fields := strings.Split(strings.TrimSuffix(line, "\n"), " ") // not Fields: an empty requirement is an empty field
		if fields[0] == "" {
			continue
		}
		values := make([]string, len(fields)-1)
		for i, f := range fields[1:] {
			b, err := hex.DecodeString(f)
			if err != nil {
				return AppAccess{}, fmt.Errorf("unreadable access record %q", fields[0])
			}
			values[i] = string(b)
		}
		var last *ACLEntry
		if len(a.Entries) > 0 {
			last = &a.Entries[len(a.Entries)-1]
		}
		switch kind := fields[0]; {
		case kind == "self" && len(values) == 3 && !self:
			a.Broker, a.Partition, self = identity(values[0], values[1]), values[2], true
		case kind == "acl":
			a.Entries = append(a.Entries, ACLEntry{Authorizations: values})
		case kind == "any" && len(values) == 0 && last != nil:
			last.Any = true
		case kind == "app" && len(values) == 2 && last != nil:
			last.Apps = append(last.Apps, identity(values[0], values[1]))
		case kind == "partition" && len(values) == 1 && last != nil:
			last.Partitions = append(last.Partitions, values[0])
		default:
			return AppAccess{}, fmt.Errorf("unexpected access record %q", kind)
		}
	}
	if !self {
		return AppAccess{}, errors.New("access lacks the broker's identity")
	}
	return a, nil
}

var appID = regexp.MustCompile(`^[1-9][0-9]{0,19}$`)

func erase(b []byte) {
	for i := range b {
		b[i] = 0
	}
}

// ClaudeLogin is the command that logs account in: login claude, NAME only for an account other than the default.
func ClaudeLogin(account string) string {
	return credential.LoginCommand(credential.StaticRef(credential.Claude, account))
}

// Login reads account's setup-token with read, checks it, stores it in account's item (replacing an earlier one) and
// makes the item trust only the broker (secureItem, asking password when needed). It then proves start's no-dialog
// read returns the token. When that read would ask, the one dialog happens here, announced on notice, and a second
// no-dialog read proves Always Allow was clicked. Neither notice nor out sees the token.
func Login(account string, read, password func() ([]byte, error), k Keychain, notice, out io.Writer) error {
	if !launcher.ValidClaudeAccount(account) {
		return fmt.Errorf("invalid Claude account name %q", account)
	}
	token, err := read()
	defer erase(token)
	if err != nil {
		return fmt.Errorf("reading the setup-token failed: %w", err)
	}
	if err := checkSecret("setup-token", token); err != nil {
		return err
	}
	if err := k.Put(account, token); err != nil {
		return err
	}
	item, rerun := tokenService+" "+account, "rerun "+ClaudeLogin(account)
	if err := secureItem(tokenService, account, rerun+" to finish", k, password); err != nil {
		return err
	}
	get := func() ([]byte, error) { return k.Get(account) }
	err = readsBack(token, item, rerun, get)
	if errors.Is(err, ErrNeedsDialog) {
		err = approveByDialog(token, item, rerun, get, func() ([]byte, error) { return k.GetWithDialog(account) }, notice)
	}
	if err != nil {
		return err
	}
	_, err = fmt.Fprintf(out, "Claude account %s setup-token saved; keychain trusts only the broker\n", account)
	return err
}

// checkSecret is nil when secret, what names it, is 1 to maxToken bytes of printable UTF-8 without whitespace.
func checkSecret(what string, secret []byte) error {
	if len(secret) == 0 || len(secret) > maxToken {
		return fmt.Errorf("%s must be 1 to %d bytes", what, maxToken)
	}
	for _, r := range string(secret) {
		if unicode.IsSpace(r) || unicode.IsControl(r) || r == unicode.ReplacementChar {
			return fmt.Errorf("%s contains whitespace or control characters", what)
		}
	}
	return nil
}

// approveByDialog moves item's one read dialog into login claude: it announces it on notice, reads with it
// (getWithDialog), then proves with a no-dialog read (get) that Always Allow, not Allow, was clicked.
func approveByDialog(token []byte, item, rerun string, get, getWithDialog func() ([]byte, error), notice io.Writer) error {
	if _, err := fmt.Fprintln(notice, "macOS will ask to let ensemble-broker use "+item+": enter the login keychain password if asked and click Always Allow"); err != nil {
		return err
	}
	if err := readsBack(token, item, rerun, getWithDialog); err != nil {
		return err
	}
	err := readsBack(token, item, rerun, get)
	if errors.Is(err, ErrNeedsDialog) {
		return fmt.Errorf("keychain item %s still needs a keychain dialog: click Always Allow, not Allow; %s", item, rerun)
	}
	return err
}

// readsBack is nil when get returns token, else why not, wrapping get's error and ending with rerun; it erases what
// get returned.
func readsBack(token []byte, item, rerun string, get func() ([]byte, error)) error {
	saved, err := get()
	defer erase(saved)
	if err == nil && !bytes.Equal(saved, token) {
		err = errors.New("it holds other bytes")
	}
	if err != nil {
		return fmt.Errorf("keychain item %s: reading it failed: %w; %s", item, err, rerun)
	}
	return nil
}

// StoreAppKey creates app's private-key item, account id, from pem: an item the broker creates trusts only it for
// reads, so LoginGitHub then needs only the partition list's password and no dialog. It refuses an existing item and
// never names pem's bytes; the caller erases pem.
func StoreAppKey(app, id string, pem []byte, k AppKeyStore) error {
	if !appID.MatchString(id) {
		return fmt.Errorf("App ID %q is not 1 to 20 digits", id)
	}
	if _, err := github.ParseKey(pem); err != nil {
		return fmt.Errorf("not a GitHub App private key: %w", err)
	}
	switch _, err := k.AppAccount(app); {
	case errors.Is(err, ErrNotFound):
	case err == nil, errors.Is(err, ErrAmbiguous):
		return fmt.Errorf("keychain item %s exists: run ensemble-broker login github %s without --key-file, or delete it first", app, app)
	default:
		return err
	}
	return k.PutSecret(app, id, pem)
}

// LoginGitHub changes app's existing private-key item's ACL in place to trust only the broker (secureItem), then
// reads the key once to confirm it is usable; the item is never deleted and its content never changes. The key is
// read only once the item trusts only the broker, so that read needs no dialog. out never sees the key or the
// password.
func LoginGitHub(app string, k AppKeychain, password func() ([]byte, error), out io.Writer) error {
	account, err := k.AppAccount(app)
	switch {
	case errors.Is(err, ErrNotFound):
		return fmt.Errorf("no keychain item with service %s: add the App private key first", app)
	case errors.Is(err, ErrAmbiguous):
		return fmt.Errorf("more than one keychain item with service %s: keep only the App private key", app)
	case err != nil:
		return err
	case !appID.MatchString(account):
		return fmt.Errorf("keychain item %s: account %q is not an App ID", app, account)
	}
	if err := secureItem(app, account, "rerun ensemble-broker login github "+app+" to finish", k, password); err != nil {
		return err
	}
	key, err := k.AppKey(app, account)
	empty := len(key) == 0
	erase(key)
	switch {
	case err != nil:
		return fmt.Errorf("keychain item %s trusts only the broker, but reading it failed: %w", app, err)
	case empty:
		return fmt.Errorf("keychain item %s trusts only the broker, but it is empty", app)
	}
	_, err = fmt.Fprintf(out, "GitHub App %s private key (App ID %s) now trusts only the broker\n", app, account)
	return err
}

// secureItem changes the item's ACL in place to trust only the broker. Each step runs only when its part is not yet
// in place: the partition list first, then the read and export entries, both with the login keychain password read
// once with password, so no keychain dialog shows unless securityd refuses the second step with the password
// (trust). Its error says what is done and not done, and ends with rerun.
func secureItem(item, account, rerun string, k ItemTrust, password func() ([]byte, error)) error {
	return secureGuarded(item, account, rerun, k, password, nil)
}

// secureGuarded is secureItem, changing an item that does not yet trust only the broker only when guard (nil: any)
// holds for the access it read first; else it fails with errNotEarlierBuild, the item unchanged, no password read.
// The guard reads the access right before the change, after the password; the change looks the item up again, so a
// same-user process could still swap it in between (accepted: spec D9).
func secureGuarded(item, account, rerun string, k ItemTrust, password func() ([]byte, error), guard func(AppAccess) bool) error {
	before, err := k.AppAccess(item, account)
	if err != nil {
		return fmt.Errorf("keychain item %s unchanged: %w: %w", item, errAccessUnread, err)
	}
	if onlyBroker(before) != nil && guard != nil && !guard(before) {
		return fmt.Errorf("keychain item %s unchanged: %w", item, errNotEarlierBuild)
	}
	if onlyBroker(before) != nil {
		pw, err := password()
		if err == nil {
			err = trust(item, account, k, before, pw)
		} else {
			err = fmt.Errorf("reading the login keychain password failed: %w", err)
		}
		erase(pw)
		if err != nil {
			if errors.Is(err, ErrInvalidOwnerEdit) {
				return ownershipError(item, account, err)
			}
			if err := incomplete(item, account, rerun, k, before, err); err != nil {
				return err
			}
		}
	}
	return incomplete(item, account, rerun, k, before, errors.New("checking its access afterwards"))
}

// trust runs each step not in place before with the keychain password, stopping at the first that fails. The
// partition step goes first: securityd takes it with the password, and it checks the password before the read
// and export entries change. securityd has refused that second change with the password (ErrAuthFailed), or,
// without UI, would have asked (ErrNeedsDialog), so then it goes through the keychain's dialog instead.
func trust(item, account string, k ItemTrust, before AppAccess, password []byte) error {
	if partitionOnlyBroker(before) != nil {
		if err := k.TrustPartition(item, account, password); err != nil {
			return fmt.Errorf("changing its partition list failed: %w", err)
		}
	}
	if appsOnlyBroker(before) != nil {
		err := k.TrustApps(item, account, password)
		if errors.Is(err, ErrAuthFailed) || errors.Is(err, ErrNeedsDialog) {
			if dialogErr := k.TrustAppsByDialog(item, account); dialogErr != nil {
				err = fmt.Errorf("%w; through the keychain dialog: %w", err, dialogErr)
			} else {
				err = nil
			}
		}
		if err != nil {
			return fmt.Errorf("changing its read and export entries failed: %w", err)
		}
	}
	return nil
}

// incomplete reads the item's access back: nil when it trusts only the broker, whatever cause says; else why the
// run stopped, whether it changed the access from before, what is in place, what is not and rerun, how to finish.
func incomplete(item, account, rerun string, k ItemTrust, before AppAccess, cause error) error {
	after, err := k.AppAccess(item, account)
	if err != nil {
		return fmt.Errorf("keychain item %s: %w; reading its access back failed: %w, so what changed is unknown; %s", item, cause, err, rerun)
	}
	appsErr, partitionErr := appsOnlyBroker(after), partitionOnlyBroker(after)
	if appsErr == nil && partitionErr == nil {
		return nil
	}
	changed := "partly changed"
	if reflect.DeepEqual(before, after) {
		changed = "unchanged"
	}
	done, todo := "nothing", errors.Join(appsErr, partitionErr).Error()
	switch {
	case appsErr == nil:
		done = "its read and export entries trust only the broker"
	case partitionErr == nil:
		done = "its partition list holds only the broker's partition ID"
	}
	return fmt.Errorf("keychain item %s %s: %w; done: %s; not done: %s; %s", item, changed, cause, done, strings.ReplaceAll(todo, "\n", "; "), rerun)
}

type Credentials struct{ Keychain TokenReader }

// Usable checks an account's access and token without prompting or exporting the token.
// It checks local usability, not revocation or expiry at the upstream service.
func (c Credentials) Usable(account string) bool {
	if c.Ready(account) != nil {
		return false
	}
	token, err := c.Keychain.Get(account)
	defer erase(token)
	return err == nil && checkSecret("setup-token", token) == nil
}

// keychainError maps a keychain failure for account's token to the protocol, without its detail.
func keychainError(account string, err error) *protocol.Error {
	switch {
	case errors.Is(err, ErrNotFound):
		return protocol.New(protocol.LoginRequired, "No Claude setup-token for account "+account+": run "+ClaudeLogin(account)+".")
	case errors.Is(err, ErrNeedsDialog):
		return protocol.New(protocol.LoginRequired, "The Claude account "+account+" setup-token needs keychain approval: unlock the login keychain, or run "+ClaudeLogin(account)+".")
	}
	return protocol.New(protocol.KeychainFailed, "Keychain access to the Claude account "+account+" setup-token failed.")
}

// Ready reads only account's item's access, so it never prompts: an item trusting another broker build would ask at
// Inject.
func (c Credentials) Ready(account string) error {
	trusted, earlier, err := checkItem(c.Keychain, tokenService, account)
	switch {
	case err != nil:
		return keychainError(account, err)
	case earlier:
		return protocol.New(protocol.LoginRequired, "The Claude account "+account+" setup-token trusts an earlier broker build: run "+RetrustCommand+".")
	case !trusted:
		return protocol.New(protocol.LoginRequired, "The Claude account "+account+" setup-token trusts another broker build: run "+ClaudeLogin(account)+".")
	}
	return nil
}

// Inject writes account's token, and no other, into the container's secrets tmpfs through exec's stdin.
// The token read never shows a keychain dialog: one it would need, or a locked keychain, fails here.
// Errors are keychainError's, wrap docker.ErrUnreachable or are opaque: no Docker stderr, no keychain detail.
func (c Credentials) Inject(account string, exec func(argv []string, stdin []byte) error) error {
	token, err := c.Keychain.Get(account)
	defer erase(token)
	if err != nil {
		return keychainError(account, err)
	}
	err = credential.Deliver(exec, credential.File{Dir: docker.SecretsDir, Name: "claude"}, token)
	if errors.Is(err, docker.ErrUnreachable) {
		return fmt.Errorf("token injection: %w", docker.ErrUnreachable)
	}
	if err != nil {
		return errors.New("writing the token into the container failed")
	}
	return nil
}
