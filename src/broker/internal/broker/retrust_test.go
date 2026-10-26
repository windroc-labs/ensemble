package broker

import (
	"bytes"
	"errors"
	"slices"
	"strings"
	"testing"
)

// retrustKeychain is a fake login keychain of items by "service/account"; each starts as given and changes as the
// real one would: the partition step sets the broker's partition ID, the apps steps the broker's identity, and the
// apps step with the password is refused (as securityd refuses it for an earlier build's app entry) when refuseApps.
type retrustKeychain struct {
	items      map[string]*AppAccess
	order      []string
	refuseApps bool
	failAccess map[string]error // AppAccess error per item
	failChange map[string]error // TrustPartition error per item
	listErr    map[string]error // Accounts or Services error per service or prefix
	steps      []string
}

func newRetrustKeychain(items map[string]*AppAccess) *retrustKeychain {
	k := &retrustKeychain{items: items, failAccess: map[string]error{}, failChange: map[string]error{}, listErr: map[string]error{}}
	for key := range items {
		k.order = append(k.order, key)
	}
	slices.Sort(k.order)
	return k
}

func split(key string) (service, account string) {
	i := strings.LastIndex(key, "/")
	return key[:i], key[i+1:]
}

func (k *retrustKeychain) Accounts(service string) ([]string, error) {
	if err := k.listErr[service]; err != nil {
		return nil, err
	}
	var accounts []string
	for _, key := range k.order {
		if s, a := split(key); s == service {
			accounts = append(accounts, a)
		}
	}
	return accounts, nil
}

func (k *retrustKeychain) Services(prefix string) ([]string, error) {
	if err := k.listErr[prefix]; err != nil {
		return nil, err
	}
	var services []string
	for _, key := range k.order {
		if s, _ := split(key); strings.HasPrefix(s, prefix) && !slices.Contains(services, s) {
			services = append(services, s)
		}
	}
	return services, nil
}

func (k *retrustKeychain) AppAccess(service, account string) (AppAccess, error) {
	key := service + "/" + account
	if err := k.failAccess[key]; err != nil {
		return AppAccess{}, err
	}
	a, ok := k.items[key]
	if !ok {
		return AppAccess{}, ErrNotFound
	}
	c := *a
	c.Entries = nil
	for _, e := range a.Entries {
		e.Authorizations, e.Apps, e.Partitions = slices.Clone(e.Authorizations), slices.Clone(e.Apps), slices.Clone(e.Partitions)
		c.Entries = append(c.Entries, e)
	}
	return c, nil
}

func (k *retrustKeychain) set(key string, f func(e *ACLEntry)) {
	for i := range k.items[key].Entries {
		f(&k.items[key].Entries[i])
	}
}

func (k *retrustKeychain) TrustPartition(service, account string, password []byte) error {
	key := service + "/" + account
	k.steps = append(k.steps, "partition "+key)
	if string(password) != keychainPass {
		return ErrAuthFailed
	}
	if err := k.failChange[key]; err != nil {
		return err
	}
	k.set(key, func(e *ACLEntry) {
		if slices.Contains(e.Authorizations, "partition_id") {
			e.Partitions = []string{brokerPartition}
		}
	})
	return nil
}

func (k *retrustKeychain) trustApps(key string) {
	k.set(key, func(e *ACLEntry) {
		if slices.ContainsFunc(e.Authorizations, func(x string) bool { return slices.Contains(readAuthorizations, x) }) {
			e.Apps = []string{brokerApp}
		}
	})
}

func (k *retrustKeychain) TrustApps(service, account string, password []byte) error {
	key := service + "/" + account
	k.steps = append(k.steps, "apps "+key)
	if k.refuseApps {
		return ErrAuthFailed
	}
	k.trustApps(key)
	return nil
}

func (k *retrustKeychain) TrustAppsByDialog(service, account string) error {
	key := service + "/" + account
	k.steps = append(k.steps, "dialog "+key)
	k.trustApps(key)
	return nil
}

// earlier is an item of a build that kept the broker's identity (stable designated requirement): only its partition
// ID is another build's.
func earlier() *AppAccess {
	a := brokerOnly()
	a.Entries[2].Partitions = []string{"cdhash:1ce471"}
	return &a
}

func current() *AppAccess {
	a := brokerOnly()
	return &a
}

// countingPassword reads keychainPass, counting reads, running before at the first.
type countingPassword struct {
	calls  int
	before func()
}

func (p *countingPassword) read() ([]byte, error) {
	if p.calls++; p.calls == 1 && p.before != nil {
		p.before()
	}
	return []byte(keychainPass), nil
}

func retrust(k *retrustKeychain, p *countingPassword) (notice, out string, err error) {
	var n, o bytes.Buffer
	err = Retrust(k, p.read, &n, &o)
	return n.String(), o.String(), err
}

func TestRetrustAsksPasswordOnce(t *testing.T) {
	k := newRetrustKeychain(map[string]*AppAccess{
		tokenService + "/default":          earlier(),
		tokenService + "/work":             earlier(),
		codexService + "/auth":             earlier(),
		tokensService + "/ensemble-w-0123": earlier(),
		revocationsService + "/pending":    earlier(),
		AppKeyService + "/1234567":         earlier(),
		usersService + "/" + AppKeyService: current(),
		codexGrantsService + "/backlog":    earlier(),
	})
	p := &countingPassword{}
	notice, out, err := retrust(k, p)
	if err != nil || p.calls != 1 {
		t.Fatalf("%v, asked %d times\n%s", err, p.calls, notice)
	}
	for key, a := range k.items {
		if onlyBroker(*a) != nil {
			t.Errorf("%s not re-trusted: %v", key, onlyBroker(*a))
		}
	}
	if slices.ContainsFunc(k.steps, func(s string) bool { return !strings.HasPrefix(s, "partition ") }) {
		t.Errorf("an earlier build with the broker's identity needs only its partition list: %q", k.steps)
	}
	if !strings.HasSuffix(out, "re-trusted 7 broker keychain item(s)\n") || !strings.Contains(out, `"ensemble-broker-claude" "work" now trusts this broker`) {
		t.Errorf("out %q", out)
	}
	if strings.Contains(notice+out, keychainPass) {
		t.Error("the password reached the output")
	}
}

func TestRetrustListsBeforePassword(t *testing.T) {
	k := newRetrustKeychain(map[string]*AppAccess{tokenService + "/default": earlier(), codexService + "/auth": earlier()})
	var n, o bytes.Buffer
	var atPassword string
	err := Retrust(k, func() ([]byte, error) { atPassword = n.String(); return []byte(keychainPass), nil }, &n, &o)
	want := "will re-trust \"ensemble-broker-claude\" \"default\"\nwill re-trust \"ensemble-broker-codex\" \"auth\"\n"
	if err != nil || atPassword != want {
		t.Fatalf("%v: before the password %q", err, atPassword)
	}
}

func TestRetrustNothingToDo(t *testing.T) {
	k := newRetrustKeychain(map[string]*AppAccess{tokenService + "/default": current(), AppKeyService + "/1": current()})
	p := &countingPassword{}
	notice, out, err := retrust(k, p)
	if err != nil || p.calls != 0 || len(k.steps) != 0 || notice != "" || out != "every broker keychain item already trusts this broker\n" {
		t.Fatalf("%v asked %d steps %q notice %q out %q", err, p.calls, k.steps, notice, out)
	}
}

func TestRetrustSkipsMissingItems(t *testing.T) {
	k := newRetrustKeychain(map[string]*AppAccess{})
	p := &countingPassword{}
	notice, out, err := retrust(k, p)
	if err != nil || p.calls != 0 || len(k.steps) != 0 || notice != "" || out != "every broker keychain item already trusts this broker\n" {
		t.Fatalf("%v asked %d steps %q notice %q out %q", err, p.calls, k.steps, notice, out)
	}
}

func TestRetrustLegacyAnnouncesDialog(t *testing.T) {
	k := newRetrustKeychain(map[string]*AppAccess{tokenService + "/default": reinstalled()})
	k.refuseApps = true
	p := &countingPassword{}
	notice, out, err := retrust(k, p)
	if err != nil || p.calls != 1 || onlyBroker(*k.items[tokenService+"/default"]) != nil {
		t.Fatalf("%v asked %d\n%s", err, p.calls, notice)
	}
	want := `macOS will ask once to let ensemble-broker change access to "ensemble-broker-claude" "default": enter the login keychain password and click Allow`
	if !strings.Contains(notice, want) || !slices.Equal(k.steps, []string{"partition ensemble-broker-claude/default", "apps ensemble-broker-claude/default", "dialog ensemble-broker-claude/default"}) {
		t.Fatalf("notice %q steps %q", notice, k.steps)
	}
	if out != "\"ensemble-broker-claude\" \"default\" now trusts this broker\nre-trusted 1 broker keychain item(s)\n" {
		t.Fatalf("out %q", out)
	}
}

// After a legacy item's one dialog its app entry is the broker's stable identity: the next build's retrust changes
// only the partition list, with no dialog.
func TestRetrustNextUpgradeIsPartitionOnly(t *testing.T) {
	k := newRetrustKeychain(map[string]*AppAccess{tokenService + "/default": reinstalled()})
	k.refuseApps = true
	if _, _, err := retrust(k, &countingPassword{}); err != nil {
		t.Fatal(err)
	}
	k.set(tokenService+"/default", func(e *ACLEntry) { // the next build: another cdhash, the same identity
		if slices.Contains(e.Authorizations, "partition_id") {
			e.Partitions = []string{"cdhash:2d2d2d"}
		}
	})
	k.steps = nil
	notice, _, err := retrust(k, &countingPassword{})
	if err != nil || !slices.Equal(k.steps, []string{"partition ensemble-broker-claude/default"}) || strings.Contains(notice, "macOS will ask") {
		t.Fatalf("%v steps %q notice %q", err, k.steps, notice)
	}
}

func TestRetrustCoversEveryBrokerItem(t *testing.T) {
	orchestrator := "linear-app-harness-secret"
	k := newRetrustKeychain(map[string]*AppAccess{
		tokenService + "/default":                              earlier(),
		codexService + "/auth":                                 earlier(),
		AppKeyService + "/1234567":                             earlier(),
		usersService + "/" + AppKeyService:                     earlier(),
		tokensService + "/ensemble-w-0123":                     earlier(),
		revocationsService + "/pending":                        earlier(),
		codexGrantsService + "/backlog":                        earlier(),
		opencodeService + "/strata":                            earlier(),
		envServicePrefix + "mini-op-password/mini-op-password": earlier(),
		envServicePrefix + "mini-op-ssh-key/mini-op-ssh-key":   earlier(),
		envServicePrefix + "Bad/Bad":                           earlier(),
		envServicePrefix + "db/other":                          earlier(),
		linearService + "/registry":                            earlier(),
		linearGrantsService + "/harness":                       earlier(),
		orchestrator + "/harness":                              earlier(),
	})
	p := &countingPassword{}
	_, out, err := retrust(k, p)
	if err != nil || p.calls != 1 || !strings.HasSuffix(out, "re-trusted 12 broker keychain item(s)\n") {
		t.Fatalf("%v asked %d out %q", err, p.calls, out)
	}
	for _, skipped := range []string{envServicePrefix + "Bad/Bad", envServicePrefix + "db/other", orchestrator + "/harness"} {
		if onlyBroker(*k.items[skipped]) == nil {
			t.Errorf("%s re-trusted", skipped)
		}
	}
}

func TestRetrustServicesAreKeychainServices(t *testing.T) {
	if !slices.Equal(retrustServices, KeychainServices) {
		t.Fatalf("retrustServices %q, KeychainServices %q", retrustServices, KeychainServices)
	}
}

func TestRetrustRejectsTeamPartition(t *testing.T) {
	a := earlier()
	a.Entries[2].Partitions = []string{"teamid:ABCDE12345"}
	k := newRetrustKeychain(map[string]*AppAccess{tokenService + "/default": a})
	p := &countingPassword{}
	notice, _, err := retrust(k, p)
	if err != nil || p.calls != 0 || len(k.steps) != 0 || !strings.Contains(notice, `"ensemble-broker-claude" "default" left unchanged: it trusts another application; run ensemble-broker login claude`) {
		t.Fatalf("%v asked %d steps %q notice %q", err, p.calls, k.steps, notice)
	}
}

func TestRetrustLeavesForeignItems(t *testing.T) {
	k := newRetrustKeychain(map[string]*AppAccess{
		tokenService + "/default":          added(),
		codexService + "/auth":             added(),
		AppKeyService + "/1":               added(),
		opencodeService + "/strata":        added(),
		envServicePrefix + "db/db":         added(),
		tokensService + "/ensemble-w-0123": added(),
		linearService + "/registry":        added(),
		revocationsService + "/pending":    earlier(),
	})
	p := &countingPassword{}
	notice, out, err := retrust(k, p)
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{
		`"ensemble-broker-claude" "default" left unchanged: it trusts another application; run ensemble-broker login claude` + "\n",
		`"ensemble-broker-codex" "auth" left unchanged: it trusts another application; run ` + codexLoginCommand + "\n",
		`"ensemble-broker-bot" "1" left unchanged: it trusts another application; run ensemble-broker login github ensemble-broker-bot` + "\n",
		`"ensemble-broker-opencode" "strata" left unchanged: it trusts another application; run ensemble-broker login opencode strata` + "\n",
		`"ensemble-broker-env/db" "db" left unchanged: it trusts another application; run ensemble-broker login env db` + "\n",
		`"ensemble-broker-tokens" "ensemble-w-0123" left unchanged: it trusts another application; delete it in Keychain Access` + "\n",
		`"ensemble-broker-linear" "registry" left unchanged: it trusts another application; run ensemble-broker login linear` + "\n",
	} {
		if !strings.Contains(notice, want) {
			t.Errorf("notice lacks %q:\n%s", want, notice)
		}
	}
	if slices.ContainsFunc(k.steps, func(s string) bool { return !strings.HasSuffix(s, revocationsService+"/pending") }) || out != "\"ensemble-broker-revocations\" \"pending\" now trusts this broker\nre-trusted 1 broker keychain item(s)\n" {
		t.Fatalf("steps %q out %q", k.steps, out)
	}
}

// An item swapped to another application's between the listing and the password is left unchanged.
func TestRetrustRechecksAfterPassword(t *testing.T) {
	k := newRetrustKeychain(map[string]*AppAccess{tokenService + "/default": earlier(), codexService + "/auth": earlier()})
	p := &countingPassword{before: func() { *k.items[codexService+"/auth"] = *added() }}
	notice, out, err := retrust(k, p)
	if err != nil || slices.ContainsFunc(k.steps, func(s string) bool { return strings.Contains(s, codexService) }) {
		t.Fatalf("%v steps %q", err, k.steps)
	}
	if !strings.Contains(notice, `"ensemble-broker-codex" "auth" left unchanged: it trusts another application`) || !strings.HasSuffix(out, "re-trusted 1 broker keychain item(s)\n") {
		t.Fatalf("notice %q out %q", notice, out)
	}
}

func TestRetrustContinuesAfterFailure(t *testing.T) {
	k := newRetrustKeychain(map[string]*AppAccess{
		tokenService + "/default": earlier(),
		tokenService + "/work":    earlier(),
		codexService + "/auth":    earlier(),
	})
	k.failChange[tokenService+"/default"] = errors.New("keychain status -25300")
	k.failAccess[codexService+"/auth"] = errors.New("keychain status -25299")
	k.listErr[envServicePrefix] = errors.New("keychain status -25291")
	notice, out, err := retrust(k, &countingPassword{})
	if err == nil || !strings.Contains(err.Error(), "rerun "+RetrustCommand) {
		t.Fatalf("err %v", err)
	}
	if onlyBroker(*k.items[tokenService+"/work"]) != nil || !strings.Contains(out, `"ensemble-broker-claude" "work" now trusts this broker`) {
		t.Fatalf("the other item was not re-trusted: %q", out)
	}
	var failures []string
	for _, line := range strings.Split(strings.TrimSpace(notice), "\n") {
		if !strings.HasPrefix(line, "will re-trust") {
			failures = append(failures, line)
		}
	}
	if len(failures) != 3 {
		t.Fatalf("want one line per failure, got %q", failures)
	}
	for _, line := range failures {
		if !strings.HasSuffix(line, "rerun "+RetrustCommand) {
			t.Errorf("%q does not end with the rerun", line)
		}
	}
}
