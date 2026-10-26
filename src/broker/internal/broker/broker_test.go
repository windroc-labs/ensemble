package broker

import (
	"bytes"
	"cmp"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/hex"
	"encoding/pem"
	"errors"
	"fmt"
	"go/ast"
	"go/parser"
	gotoken "go/token"
	"io"
	"maps"
	"path/filepath"
	"reflect"
	"slices"
	"strconv"
	"strings"
	"testing"

	"ensemble/broker/internal/credential"
	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/launcher"
	"ensemble/broker/internal/protocol"
)

// fakeKeychain is one setup-token item, service tokenService and account account ("": default): its access is
// fakeApps', its reads return token (readBack when set). A call naming another item fails.
type fakeKeychain struct {
	fakeApps
	account          string
	token, readBack  []byte
	getErr           error
	getErrs          []error // the no-dialog reads' errors in turn, then getErr
	dialogErr        error
	put              []byte
	gets, dialogGets int
	returned         [][]byte // every buffer a read returned
}

var errWrongItem = errors.New("wrong item")

func (k *fakeKeychain) is(service, account string) bool {
	return service == tokenService && account == cmp.Or(k.account, "default")
}
func (k *fakeKeychain) AppAccess(service, account string) (AppAccess, error) {
	if !k.is(service, account) {
		return AppAccess{}, errWrongItem
	}
	return k.fakeApps.AppAccess(service, account)
}
func (k *fakeKeychain) Get(account string) ([]byte, error) {
	k.gets++
	err := k.getErr
	if len(k.getErrs) > 0 {
		err, k.getErrs = k.getErrs[0], k.getErrs[1:]
	}
	if !k.is(tokenService, account) {
		err = errWrongItem
	}
	return k.read(err)
}
func (k *fakeKeychain) GetWithDialog(account string) ([]byte, error) {
	k.dialogGets++
	err := k.dialogErr
	if !k.is(tokenService, account) {
		err = errWrongItem
	}
	return k.read(err)
}
func (k *fakeKeychain) read(err error) ([]byte, error) {
	if err != nil {
		return nil, err
	}
	b := append([]byte(nil), k.token...)
	if k.readBack != nil {
		b = append([]byte(nil), k.readBack...)
	}
	k.returned = append(k.returned, b)
	return b, nil
}
func (k *fakeKeychain) Put(account string, b []byte) error {
	if !k.is(tokenService, account) {
		return errWrongItem
	}
	k.put, k.token = append([]byte(nil), b...), append([]byte(nil), b...)
	return nil
}

// accounts is the login keychain holding one fakeKeychain item per account: each call goes to its account's item.
type accounts map[string]*fakeKeychain

func (a accounts) item(account string) *fakeKeychain {
	if k := a[account]; k != nil {
		return k
	}
	return &fakeKeychain{account: account, getErr: ErrNotFound, fakeApps: fakeApps{accessErr: ErrNotFound}}
}
func (a accounts) AppAccess(service, account string) (AppAccess, error) {
	return a.item(account).AppAccess(service, account)
}
func (a accounts) Get(account string) ([]byte, error) { return a.item(account).Get(account) }
func (a accounts) GetWithDialog(account string) ([]byte, error) {
	return a.item(account).GetWithDialog(account)
}
func (a accounts) Put(account string, b []byte) error {
	if a[account] == nil {
		a[account] = &fakeKeychain{account: account}
	}
	return a[account].Put(account, b)
}
func (a accounts) TrustApps(service, account string, password []byte) error {
	return a.item(account).TrustApps(service, account, password)
}
func (a accounts) TrustAppsByDialog(service, account string) error {
	return a.item(account).TrustAppsByDialog(service, account)
}
func (a accounts) TrustPartition(service, account string, password []byte) error {
	return a.item(account).TrustPartition(service, account, password)
}

// erased is nil when every buffer a read returned is all zeros.
func (k *fakeKeychain) erased() error {
	for _, b := range k.returned {
		if !bytes.Equal(b, make([]byte, len(b))) {
			return errors.New("read buffer not erased")
		}
	}
	return nil
}

type fakeExec struct {
	argv  [][]string
	stdin [][]byte
	err   error
}

func (e *fakeExec) run(argv []string, stdin []byte) error {
	e.argv = append(e.argv, argv)
	e.stdin = append(e.stdin, append([]byte(nil), stdin...))
	return e.err
}

const token = "sk-ant-oat01-SECRET"

const claudeSaved = "Claude account default setup-token saved; keychain trusts only the broker\n"

func TestLogin(t *testing.T) {
	for _, bad := range []string{"", "has space", "tab\there", "nul\x00", strings.Repeat("a", 16385)} {
		k := &fakeKeychain{}
		ask := &askOnce{}
		var out bytes.Buffer
		err := Login("default", func() ([]byte, error) { return []byte(bad), nil }, ask.read, k, &out, &out)
		if err == nil || k.put != nil || ask.calls != 0 || strings.Contains(err.Error(), bad) && bad != "" {
			t.Fatalf("%q accepted or echoed: %v", bad, err)
		}
	}
	k := &fakeKeychain{}
	ask := &askOnce{}
	var notice, out bytes.Buffer
	read := []byte(token)
	if err := Login("default", func() ([]byte, error) { return read, nil }, ask.read, k, &notice, &out); err != nil {
		t.Fatal(err)
	}
	if string(k.put) != token || out.String() != claudeSaved || notice.Len() != 0 {
		t.Fatalf("%q %q %q", k.put, out.String(), notice.String())
	}
	if ask.calls != 0 || len(k.steps) != 0 || k.gets != 1 || k.dialogGets != 0 {
		t.Fatalf("asked %d, steps %q, reads %d, dialog reads %d", ask.calls, k.steps, k.gets, k.dialogGets)
	}
	if !bytes.Equal(read, make([]byte, len(read))) {
		t.Fatal("token buffer not erased")
	}
	if err := k.erased(); err != nil {
		t.Fatal(err)
	}
	if err := Login("default", func() ([]byte, error) { return nil, errors.New("needs an interactive terminal") }, ask.read, &fakeKeychain{}, &out, &out); err == nil || err.Error() != "reading the setup-token failed: needs an interactive terminal" {
		t.Fatalf("read error: %v", err)
	}
}

// loginClaude runs Login with token and the password reader ask; it fails t when a secret reaches an error or output.
func loginClaude(t *testing.T, k *fakeKeychain, ask *askOnce) (notice string, err error) {
	t.Helper()
	var n, out bytes.Buffer
	err = Login("default", func() ([]byte, error) { return []byte(token), nil }, ask.read, k, &n, &out)
	text := n.String() + out.String()
	if err != nil {
		text += err.Error()
	}
	if strings.Contains(text, token) || strings.Contains(text, keychainPass) {
		t.Fatalf("secret in %q", text)
	}
	if err == nil && out.String() != claudeSaved {
		t.Fatalf("output %q", out.String())
	}
	if err := k.erased(); err != nil {
		t.Fatal(err)
	}
	return n.String(), err
}

func TestLoginTrustsOnlyBroker(t *testing.T) {
	const item = " ensemble-broker-claude default"
	for name, tc := range map[string]struct {
		access          *AppAccess
		appsErr, askErr error
		partitionErr    error
		steps           []string
		fails           string
	}{
		"reinstalled":     {access: reinstalled(), steps: []string{"partition", "apps"}},
		"dialog fallback": {access: added(), appsErr: ErrAuthFailed, steps: []string{"partition", "apps", "apps by dialog"}},
		"refused":         {access: reinstalled(), partitionErr: errors.New("keychain status -25293"), steps: []string{"partition"}, fails: "changing its partition list failed"},
		"no password":     {access: reinstalled(), askErr: errors.New("needs an interactive terminal"), fails: "reading the login keychain password failed"},
	} {
		k := &fakeKeychain{fakeApps: fakeApps{access: tc.access, appsErr: tc.appsErr, partitionErr: tc.partitionErr}}
		ask := &askOnce{err: tc.askErr}
		_, err := loginClaude(t, k, ask)
		var steps []string
		for _, s := range k.steps {
			steps = append(steps, strings.TrimSuffix(s, item))
		}
		if ask.calls != 1 || !slices.Equal(steps, tc.steps) {
			t.Fatalf("%s: asked %d, steps %q", name, ask.calls, k.steps)
		}
		if tc.fails == "" {
			if err != nil || k.gets != 1 || !slices.Equal(k.passwordSeen[len(k.passwordSeen)-1:], []string{keychainPass}) {
				t.Fatalf("%s: %v, reads %d", name, err, k.gets)
			}
			continue
		}
		if err == nil || !strings.Contains(err.Error(), "keychain item ensemble-broker-claude") || !strings.Contains(err.Error(), tc.fails) ||
			!strings.HasSuffix(err.Error(), "rerun ensemble-broker login claude to finish") || k.gets != 0 {
			t.Fatalf("%s: %v, reads %d", name, err, k.gets)
		}
	}
}

// A no-dialog read that would ask moves the one dialog into login claude, announced, and proves it lasts.
func TestLoginNeedsDialog(t *testing.T) {
	const announce = "macOS will ask to let ensemble-broker use ensemble-broker-claude default: enter the login keychain password if asked and click Always Allow\n"
	for name, tc := range map[string]struct {
		getErrs   []error
		dialogErr error
		readBack  string
		dialog    bool
		fails     string
	}{
		"always allowed": {getErrs: []error{ErrNeedsDialog}, dialog: true},
		"allowed once":   {getErrs: []error{ErrNeedsDialog, ErrNeedsDialog}, dialog: true, fails: "keychain item ensemble-broker-claude default still needs a keychain dialog: click Always Allow, not Allow; rerun ensemble-broker login claude"},
		"denied":         {getErrs: []error{ErrNeedsDialog}, dialogErr: errors.New("keychain status -128"), dialog: true, fails: "keychain item ensemble-broker-claude default: reading it failed: keychain status -128; rerun ensemble-broker login claude"},
		"other bytes":    {readBack: "sk-ant-other", fails: "keychain item ensemble-broker-claude default: reading it failed: it holds other bytes; rerun ensemble-broker login claude"},
		"read error":     {getErrs: []error{errors.New("keychain status -25300")}, fails: "keychain item ensemble-broker-claude default: reading it failed: keychain status -25300; rerun ensemble-broker login claude"},
	} {
		k := &fakeKeychain{getErrs: tc.getErrs, dialogErr: tc.dialogErr}
		if tc.readBack != "" {
			k.readBack = []byte(tc.readBack)
		}
		ask := &askOnce{}
		notice, err := loginClaude(t, k, ask)
		if tc.fails == "" && err != nil || tc.fails != "" && (err == nil || err.Error() != tc.fails) {
			t.Fatalf("%s: %v", name, err)
		}
		if wantNotice := map[bool]string{true: announce}[tc.dialog]; notice != wantNotice || k.dialogGets != map[bool]int{true: 1}[tc.dialog] || ask.calls != 0 {
			t.Fatalf("%s: notice %q, dialog reads %d, asked %d", name, notice, k.dialogGets, ask.calls)
		}
	}
}

// Two accounts log in separately: each its own ensemble-broker-claude item, its own token, trusting only the broker.
func TestLoginAccounts(t *testing.T) {
	k := accounts{"default": {fakeApps: fakeApps{access: reinstalled()}}}
	tokens := map[string]string{"default": token, "work": "sk-ant-oat01-WORK"}
	for _, account := range []string{"default", "work"} {
		ask := &askOnce{}
		var notice, out bytes.Buffer
		err := Login(account, func() ([]byte, error) { return []byte(tokens[account]), nil }, ask.read, k, &notice, &out)
		text := notice.String() + out.String()
		if err != nil {
			text += err.Error()
		}
		if err != nil || out.String() != "Claude account "+account+" setup-token saved; keychain trusts only the broker\n" || notice.Len() != 0 {
			t.Fatalf("%s: %v %q %q", account, err, out.String(), notice.String())
		}
		if strings.Contains(text, token) || strings.Contains(text, tokens["work"]) || strings.Contains(text, keychainPass) {
			t.Fatalf("%s: secret in %q", account, text)
		}
	}
	for account, want := range tokens {
		item := k[account]
		if item == nil || string(item.token) != want || item.gets != 1 {
			t.Fatalf("%s: item %+v", account, item)
		}
		if access, err := item.fakeApps.AppAccess(tokenService, account); err != nil || onlyBroker(access) != nil {
			t.Fatalf("%s: access %v %v", account, onlyBroker(access), err)
		}
		if err := item.erased(); err != nil {
			t.Fatal(err)
		}
	}
	if steps := k["default"].steps; !slices.Equal(steps, []string{"partition ensemble-broker-claude default", "apps ensemble-broker-claude default"}) {
		t.Fatalf("default steps %q", steps)
	}
	if steps := k["work"].steps; len(steps) != 0 {
		t.Fatalf("work steps %q", steps)
	}
	k["work"].getErrs = []error{errors.New("keychain status -25300")}
	err := Login("work", func() ([]byte, error) { return []byte(tokens["work"]), nil }, (&askOnce{}).read, k, io.Discard, io.Discard)
	if err == nil || err.Error() != "keychain item ensemble-broker-claude work: reading it failed: keychain status -25300; rerun ensemble-broker login claude work" {
		t.Fatalf("work rerun: %v", err)
	}
	for _, bad := range []string{"Work", "setup-token", "", "a/b", "-x", strings.Repeat("a", 65)} {
		reads, ask, k := 0, &askOnce{}, accounts{}
		err := Login(bad, func() ([]byte, error) { reads++; return []byte(token), nil }, ask.read, k, io.Discard, io.Discard)
		if err == nil || err.Error() != fmt.Sprintf("invalid Claude account name %q", bad) || reads != 0 || ask.calls != 0 || len(k) != 0 {
			t.Fatalf("%q: %v, reads %d, asked %d, items %d", bad, err, reads, ask.calls, len(k))
		}
	}
}

func TestClaudeLogin(t *testing.T) {
	if got := ClaudeLogin("default"); got != "ensemble-broker login claude" {
		t.Fatal(got)
	}
	if got := ClaudeLogin("work"); got != "ensemble-broker login claude work" {
		t.Fatal(got)
	}
}

// fakeApps is one App private-key item whose access TrustApps and TrustPartition change like securityd,
// unless frozen; every AppAccess returns a fresh copy, as a real read does.
type fakeApps struct {
	account, key           string
	accountErr, getErr     error
	access                 *AppAccess
	accessErr, readBackErr error
	appsErr, partitionErr  error
	dialogErr              error
	frozen                 bool
	steps                  []string
	password               []byte
	passwordSeen           []string
	got                    []byte
	reads                  int
}

const (
	brokerPartition = "cdhash:0a1b2c"
	brokerReq       = `cdhash H"0a1b2c"`
	brokerApp       = InstalledPath + " (" + brokerReq + ")"
	security        = "/usr/bin/security"
	keychainPass    = "login-keychain-password"
)

// brokerOnly is the ensemble-broker-claude item's shape: an owner entry, one decrypt entry with only the broker,
// the partition list with only the broker's partition ID.
func brokerOnly() AppAccess {
	return AppAccess{
		Broker:    brokerApp,
		Partition: brokerPartition,
		Entries: []ACLEntry{
			{Authorizations: []string{"change_acl"}},
			{Authorizations: []string{"encrypt", "decrypt", "derive", "export_clear", "export_wrapped"}, Apps: []string{brokerApp}},
			{Authorizations: []string{"partition_id"}, Any: true, Partitions: []string{brokerPartition}},
		},
	}
}

// added is an item as security add-generic-password leaves it.
func added() *AppAccess {
	a := brokerOnly()
	a.Entries[1].Apps = []string{security}
	a.Entries[2].Partitions = []string{"apple-tool:"}
	return &a
}

// halfDone is the item an older broker left on the user's host: read entries changed, partition list not.
func halfDone() *AppAccess {
	a := brokerOnly()
	a.Entries[2].Partitions = []string{"apple-tool:"}
	return &a
}

// reinstalled is that item after the broker is rebuilt and reinstalled: same path, another cdhash.
func reinstalled() *AppAccess {
	a := brokerOnly()
	a.Entries[1].Apps = []string{InstalledPath + ` (cdhash H"1ce471")`}
	a.Entries[2].Partitions = []string{"cdhash:1ce471"}
	return &a
}

func (k *fakeApps) AppAccess(app, account string) (AppAccess, error) {
	if k.access == nil {
		a := brokerOnly()
		k.access = &a
	}
	if k.reads++; k.reads > 1 && k.readBackErr != nil {
		return AppAccess{}, k.readBackErr
	}
	a := *k.access
	a.Entries = nil
	for _, e := range k.access.Entries {
		e.Authorizations, e.Apps, e.Partitions = slices.Clone(e.Authorizations), slices.Clone(e.Apps), slices.Clone(e.Partitions)
		a.Entries = append(a.Entries, e)
	}
	return a, k.accessErr
}

func (k *fakeApps) AppAccount(app string) (string, error) { return k.account, k.accountErr }
func (k *fakeApps) AppKey(app, account string) ([]byte, error) {
	k.steps = append(k.steps, "key "+app+" "+account)
	if account != k.account {
		return nil, ErrNotFound
	}
	k.got = []byte(k.key)
	return k.got, k.getErr
}

func (k *fakeApps) TrustApps(app, account string, password []byte) error {
	k.steps = append(k.steps, "apps "+app+" "+account)
	k.password, k.passwordSeen = password, append(k.passwordSeen, string(password))
	if k.appsErr != nil || k.frozen {
		return k.appsErr
	}
	k.trustApps()
	return nil
}

func (k *fakeApps) trustApps() {
	for i, e := range k.access.Entries {
		if slices.ContainsFunc(e.Authorizations, func(x string) bool { return slices.Contains(readAuthorizations, x) }) {
			k.access.Entries[i].Any, k.access.Entries[i].Apps = false, []string{brokerApp}
		}
	}
}

func (k *fakeApps) TrustAppsByDialog(app, account string) error {
	k.steps = append(k.steps, "apps by dialog "+app+" "+account)
	if k.dialogErr != nil || k.frozen {
		return k.dialogErr
	}
	k.trustApps()
	return nil
}

func (k *fakeApps) TrustPartition(app, account string, password []byte) error {
	k.steps = append(k.steps, "partition "+app+" "+account)
	k.password, k.passwordSeen = password, append(k.passwordSeen, string(password))
	if k.partitionErr != nil || k.frozen {
		return k.partitionErr
	}
	for i, e := range k.access.Entries {
		if slices.Contains(e.Authorizations, "partition_id") {
			k.access.Entries[i].Partitions = []string{brokerPartition}
		}
	}
	return nil
}

// askOnce is a password reader that counts its calls; it fails with err when set.
type askOnce struct {
	calls int
	err   error
}

func (p *askOnce) read() ([]byte, error) {
	p.calls++
	if p.err != nil {
		return nil, p.err
	}
	return []byte(keychainPass), nil
}

const appKey = "LS0tLS1CRUdJTiBSU0EgUFJJVkFURSBLRVktLS0tLQ=="

func TestLoginGitHub(t *testing.T) {
	for name, tc := range map[string]struct {
		access *AppAccess
		steps  []string
		asks   int
	}{
		"added":        {access: added(), steps: []string{"partition", "apps", "key"}, asks: 1},
		"half done":    {access: halfDone(), steps: []string{"partition", "key"}, asks: 1},
		"already done": {steps: []string{"key"}},
		"reinstalled":  {access: reinstalled(), steps: []string{"partition", "apps", "key"}, asks: 1},
	} {
		k := &fakeApps{account: "1234567", key: appKey, access: tc.access}
		ask := &askOnce{}
		var out bytes.Buffer
		if err := LoginGitHub("ensemble-broker-bot", k, ask.read, &out); err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		var steps []string
		for _, s := range k.steps {
			steps = append(steps, strings.TrimSuffix(s, " ensemble-broker-bot 1234567"))
		}
		if !slices.Equal(steps, tc.steps) || ask.calls != tc.asks {
			t.Fatalf("%s: steps %q, asked %d times", name, k.steps, ask.calls)
		}
		if out.String() != "GitHub App ensemble-broker-bot private key (App ID 1234567) now trusts only the broker\n" {
			t.Fatalf("%s: %q", name, out.String())
		}
		if !bytes.Equal(k.got, make([]byte, len(appKey))) {
			t.Fatalf("%s: key buffer not erased", name)
		}
		if len(k.passwordSeen) != len(tc.steps)-1 || slices.ContainsFunc(k.passwordSeen, func(p string) bool { return p != keychainPass }) {
			t.Fatalf("%s: steps got passwords %q", name, k.passwordSeen)
		}
		if tc.asks > 0 && !bytes.Equal(k.password, make([]byte, len(keychainPass))) {
			t.Fatalf("%s: password buffer after %q", name, k.password)
		}
		if !reflect.DeepEqual(*k.access, brokerOnly()) {
			t.Fatalf("%s: left %#v", name, *k.access)
		}
		var again bytes.Buffer
		if err := LoginGitHub("ensemble-broker-bot", k, ask.read, &again); err != nil || ask.calls != tc.asks || again.String() != out.String() {
			t.Fatalf("%s: rerun %v, asked %d times, %q", name, err, ask.calls, again.String())
		}
	}
	for name, k := range map[string]*fakeApps{
		"missing":   {accountErr: ErrNotFound},
		"ambiguous": {accountErr: ErrAmbiguous},
		"keychain":  {accountErr: errors.New("keychain status -25308")},
		"not an id": {account: "setup-token", key: appKey},
		"zero id":   {account: "0123", key: appKey},
		"access":    {account: "1234567", key: appKey, access: added(), accessErr: errors.New("keychain status -25293")},
	} {
		ask := &askOnce{}
		var out bytes.Buffer
		err := LoginGitHub("ensemble-broker-bot", k, ask.read, &out)
		if err == nil || k.steps != nil || ask.calls != 0 || out.Len() != 0 || strings.Contains(err.Error(), appKey) {
			t.Fatalf("%s: %v steps %q out %q", name, err, k.steps, out.String())
		}
	}
}

// The key is read only once the item trusts only the broker, so the read needs no dialog.
func TestLoginGitHubKeyAfterAccess(t *testing.T) {
	for name, tc := range map[string]struct {
		k    *fakeApps
		want string
	}{
		"read failed": {
			k:    &fakeApps{key: appKey, getErr: errors.New("keychain status -128")},
			want: "keychain item ensemble-broker-bot trusts only the broker, but reading it failed: keychain status -128",
		},
		"empty": {k: &fakeApps{}, want: "keychain item ensemble-broker-bot trusts only the broker, but it is empty"},
	} {
		tc.k.account, tc.k.access = "1234567", added()
		ask := &askOnce{}
		var out bytes.Buffer
		err := LoginGitHub("ensemble-broker-bot", tc.k, ask.read, &out)
		if err == nil || err.Error() != tc.want || ask.calls != 1 || out.Len() != 0 {
			t.Fatalf("%s: %v asked %d times out %q", name, err, ask.calls, out.String())
		}
		if !slices.Equal(tc.k.steps, []string{"partition ensemble-broker-bot 1234567", "apps ensemble-broker-bot 1234567", "key ensemble-broker-bot 1234567"}) {
			t.Fatalf("%s: steps %q", name, tc.k.steps)
		}
		if !bytes.Equal(tc.k.got, make([]byte, len(tc.k.got))) {
			t.Fatalf("%s: key buffer not erased", name)
		}
	}
}

// fakeAppStore is an empty keychain for the App key: PutSecret creates the item as secret_put leaves it, read and
// export entries trusting only the broker, partition list not yet (halfDone).
type fakeAppStore struct {
	fakeApps
	puts []string
}

func (k *fakeAppStore) PutSecret(service, account string, data []byte) error {
	k.puts = append(k.puts, service+" "+account)
	k.account, k.accountErr, k.key, k.access = account, nil, string(data), halfDone()
	return nil
}

func TestStoreAppKey(t *testing.T) {
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	pemKey := pem.EncodeToMemory(&pem.Block{Type: "RSA PRIVATE KEY", Bytes: x509.MarshalPKCS1PrivateKey(key)})
	k := &fakeAppStore{fakeApps: fakeApps{accountErr: ErrNotFound}}
	if err := StoreAppKey("ensemble-broker-bot", "1234567", pemKey, k); err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(k.puts, []string{"ensemble-broker-bot 1234567"}) || k.key != string(pemKey) {
		t.Fatalf("puts %q", k.puts)
	}
	ask := &askOnce{}
	var out bytes.Buffer
	if err := LoginGitHub("ensemble-broker-bot", k, ask.read, &out); err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(k.steps, []string{"partition ensemble-broker-bot 1234567", "key ensemble-broker-bot 1234567"}) || ask.calls != 1 {
		t.Fatalf("steps %q, asked %d times", k.steps, ask.calls)
	}
	for name, tc := range map[string]struct {
		id, pem string
		k       *fakeAppStore
		want    string
	}{
		"zero id":   {id: "0123", pem: string(pemKey), want: `App ID "0123" is not 1 to 20 digits`},
		"word id":   {id: "-w", pem: string(pemKey), want: `App ID "-w" is not 1 to 20 digits`},
		"not pem":   {id: "1", pem: "SENTINEL", want: "not a GitHub App private key: App private key is not PEM"},
		"exists":    {id: "1", pem: string(pemKey), k: &fakeAppStore{fakeApps: fakeApps{account: "9"}}, want: "keychain item ensemble-broker-bot exists: run ensemble-broker login github ensemble-broker-bot without --key-file, or delete it first"},
		"ambiguous": {id: "1", pem: string(pemKey), k: &fakeAppStore{fakeApps: fakeApps{accountErr: ErrAmbiguous}}, want: "keychain item ensemble-broker-bot exists: run ensemble-broker login github ensemble-broker-bot without --key-file, or delete it first"},
		"keychain":  {id: "1", pem: string(pemKey), k: &fakeAppStore{fakeApps: fakeApps{accountErr: errors.New("keychain status -25308")}}, want: "keychain status -25308"},
	} {
		if tc.k == nil {
			tc.k = &fakeAppStore{fakeApps: fakeApps{accountErr: ErrNotFound}}
		}
		err := StoreAppKey("ensemble-broker-bot", tc.id, []byte(tc.pem), tc.k)
		if err == nil || err.Error() != tc.want || len(tc.k.puts) != 0 || strings.Contains(err.Error(), "PRIVATE KEY") {
			t.Fatalf("%s: %v, puts %q", name, err, tc.k.puts)
		}
	}
}

func TestLoginGitHubStepFails(t *testing.T) {
	const (
		apps      = "done: its read and export entries trust only the broker"
		partition = `its partition list is ["apple-tool:"], not ["cdhash:0a1b2c"]`
		security  = `entry trusts ["/usr/bin/security"]`
		rerun     = "rerun ensemble-broker login github ensemble-broker-bot"
	)
	denied := errors.New("keychain status -25300")
	for name, tc := range map[string]struct {
		k     *fakeApps
		ask   *askOnce
		asks  int
		wants []string
	}{
		"partition first": {
			k:     &fakeApps{access: added(), partitionErr: ErrAuthFailed},
			asks:  1,
			wants: []string{"ensemble-broker-bot unchanged: changing its partition list failed: keychain status -25293", "done: nothing", security, partition, rerun},
		},
		"partition on half done": {
			k:     &fakeApps{access: halfDone(), partitionErr: denied},
			asks:  1,
			wants: []string{"ensemble-broker-bot unchanged: changing its partition list failed", apps, "not done: " + partition, rerun},
		},
		"apps after partition": {
			k:     &fakeApps{access: added(), appsErr: denied},
			asks:  1,
			wants: []string{"ensemble-broker-bot partly changed: changing its read and export entries failed: keychain status -25300", "done: its partition list holds only the broker's partition ID", security, rerun},
		},
		"apps by dialog after refusal": {
			k:     &fakeApps{access: added(), appsErr: ErrAuthFailed, dialogErr: errors.New("keychain status -128")},
			asks:  1,
			wants: []string{"ensemble-broker-bot partly changed: changing its read and export entries failed: keychain status -25293; through the keychain dialog: keychain status -128", "done: its partition list", security, rerun},
		},
		"password read": {
			k:     &fakeApps{access: added()},
			ask:   &askOnce{err: errors.New("needs an interactive terminal")},
			asks:  1,
			wants: []string{"ensemble-broker-bot unchanged: reading the login keychain password failed: needs an interactive terminal", "done: nothing", security, partition, rerun},
		},
		"read back": {
			k:     &fakeApps{access: added(), partitionErr: denied, readBackErr: errors.New("keychain status -25308")},
			asks:  1,
			wants: []string{"ensemble-broker-bot: changing its partition list failed: keychain status -25300", "reading its access back failed: keychain status -25308", "unknown", rerun},
		},
	} {
		tc.k.account, tc.k.key = "1234567", appKey
		if tc.ask == nil {
			tc.ask = &askOnce{}
		}
		var out bytes.Buffer
		err := LoginGitHub("ensemble-broker-bot", tc.k, tc.ask.read, &out)
		if err == nil || out.Len() != 0 || tc.ask.calls != tc.asks || slices.ContainsFunc(tc.k.steps, func(s string) bool { return strings.HasPrefix(s, "key ") }) {
			t.Fatalf("%s: %v out %q asked %d times, steps %q", name, err, out.String(), tc.ask.calls, tc.k.steps)
		}
		for _, want := range tc.wants {
			if !strings.Contains(err.Error(), want) {
				t.Fatalf("%s: %v lacks %q", name, err, want)
			}
		}
		if strings.Contains(err.Error(), keychainPass) || strings.Contains(err.Error(), appKey) {
			t.Fatalf("%s: %v", name, err)
		}
		if tc.k.password != nil && !bytes.Equal(tc.k.password, make([]byte, len(keychainPass))) {
			t.Fatalf("%s: password buffer not erased", name)
		}
	}
}

// securityd can refuse the read and export entries' change with the password (seen on macOS with the partition
// list not yet holding the broker): the keychain's dialog is the fallback, after the partition step.
func TestLoginGitHubAppsDialogFallback(t *testing.T) {
	for name, appsErr := range map[string]error{"refused": ErrAuthFailed, "other": errors.New("keychain status -25300")} {
		k := &fakeApps{account: "1234567", key: appKey, access: added(), appsErr: appsErr}
		ask := &askOnce{}
		var out bytes.Buffer
		err := LoginGitHub("ensemble-broker-bot", k, ask.read, &out)
		want := []string{"partition ensemble-broker-bot 1234567", "apps ensemble-broker-bot 1234567"}
		if name == "refused" {
			want = append(want, "apps by dialog ensemble-broker-bot 1234567", "key ensemble-broker-bot 1234567")
		}
		if (err == nil) != (name == "refused") || !slices.Equal(k.steps, want) || ask.calls != 1 {
			t.Fatalf("%s: %v steps %q asked %d times", name, err, k.steps, ask.calls)
		}
	}
}

// A step whose error comes with the change applied passes on the access read back.
func TestLoginGitHubStepErrorButDone(t *testing.T) {
	k := &fakeApps{account: "1234567", key: appKey, access: added()}
	var out bytes.Buffer
	err := LoginGitHub("ensemble-broker-bot", &appliedThenFailed{k}, (&askOnce{}).read, &out)
	if err != nil || out.String() != "GitHub App ensemble-broker-bot private key (App ID 1234567) now trusts only the broker\n" {
		t.Fatalf("%v %q", err, out.String())
	}
}

type appliedThenFailed struct{ *fakeApps }

func (k *appliedThenFailed) TrustApps(app, account string, password []byte) error {
	if err := k.fakeApps.TrustApps(app, account, password); err != nil {
		return err
	}
	return errors.New("keychain status -25300")
}

// Steps that report success but leave the access as it was must not pass for success.
func TestLoginGitHubVerifiesAccess(t *testing.T) {
	with := func(change func(*AppAccess)) *AppAccess {
		a := brokerOnly()
		change(&a)
		return &a
	}
	decrypt := []string{"decrypt"}
	for name, tc := range map[string]struct {
		access *AppAccess
		want   string
	}{
		"observed": {access: with(func(a *AppAccess) {
			a.Entries = []ACLEntry{
				{Authorizations: []string{"change_acl"}},
				{Authorizations: decrypt, Apps: []string{security}},
				{Authorizations: decrypt, Apps: []string{brokerApp}},
				{Authorizations: []string{"partition_id"}, Any: true, Partitions: []string{"apple-tool:"}},
			}
		}), want: security},
		"stale broker":           {access: with(func(a *AppAccess) { a.Entries[1].Apps = reinstalled().Entries[1].Apps }), want: `(cdhash H\"1ce471\")`},
		"security beside broker": {access: with(func(a *AppAccess) { a.Entries[1].Apps = []string{brokerApp, security} }), want: security},
		"any application":        {access: with(func(a *AppAccess) { a.Entries[1] = ACLEntry{Authorizations: decrypt, Any: true} }), want: "any application"},
		"any authorization": {access: with(func(a *AppAccess) {
			a.Entries = append(a.Entries, ACLEntry{Authorizations: []string{"any"}, Apps: []string{security}})
		}), want: security},
		"export to another app": {access: with(func(a *AppAccess) {
			a.Entries = append(a.Entries, ACLEntry{Authorizations: []string{"export_clear"}, Apps: []string{security}})
		}), want: security},
		"no application": {access: with(func(a *AppAccess) { a.Entries[1].Apps = nil }), want: "no application"},
		"no decrypt entry": {access: with(func(a *AppAccess) {
			a.Entries = []ACLEntry{a.Entries[0], a.Entries[2]}
		}), want: "no entry lets the broker read"},
		"apple-tool partition": {access: with(func(a *AppAccess) { a.Entries[2].Partitions = []string{"apple-tool:"} }), want: "apple-tool:"},
		"extra partition": {access: with(func(a *AppAccess) {
			a.Entries[2].Partitions = []string{brokerPartition, "apple:"}
		}), want: "apple:"},
		"no partition list": {access: with(func(a *AppAccess) { a.Entries = a.Entries[:2] }), want: "partition lists"},
		"two partition lists": {access: with(func(a *AppAccess) {
			a.Entries = append(a.Entries, a.Entries[2])
		}), want: "partition lists"},
	} {
		k := &fakeApps{account: "1234567", key: appKey, access: tc.access, frozen: true}
		var out bytes.Buffer
		err := LoginGitHub("ensemble-broker-bot", k, (&askOnce{}).read, &out)
		if err == nil || out.Len() != 0 || strings.Contains(err.Error(), appKey) {
			t.Fatalf("%s: %v out %q", name, err, out.String())
		}
		for _, want := range []string{"keychain item ensemble-broker-bot unchanged", tc.want, "rerun ensemble-broker login github ensemble-broker-bot"} {
			if !strings.Contains(err.Error(), want) {
				t.Fatalf("%s: %v lacks %q", name, err, want)
			}
		}
	}
}

func TestParseAccess(t *testing.T) {
	h := func(s string) string { return hex.EncodeToString([]byte(s)) }
	text := strings.Join([]string{
		"self " + h(InstalledPath) + " " + h(brokerReq) + " " + h(brokerPartition),
		"acl " + h("change_acl"),
		"acl " + h("encrypt") + " " + h("decrypt") + " " + h("derive") + " " + h("export_clear") + " " + h("export_wrapped"),
		"app " + h(InstalledPath) + " " + h(brokerReq),
		"acl " + h("partition_id"),
		"any",
		"partition " + h(brokerPartition),
		"",
	}, "\n")
	got, err := parseAccess([]byte(text))
	if err != nil || !reflect.DeepEqual(got, brokerOnly()) {
		t.Fatalf("%v\n%#v", err, got)
	}
	unsigned := "self " + h(InstalledPath) + " " + h(brokerReq) + " " + h(brokerPartition) + "\nacl " + h("decrypt") + "\napp " + h("/opt/tool") + " "
	got, err = parseAccess([]byte(unsigned))
	if err != nil || !slices.Equal(got.Entries[0].Apps, []string{"/opt/tool"}) {
		t.Fatalf("app without requirement: %v %#v", err, got)
	}
	for _, bad := range []string{
		"",
		"acl " + h("decrypt"),
		"self " + h(InstalledPath) + " " + h(brokerPartition),
		"self " + h(InstalledPath) + " " + h(brokerReq) + " " + h(brokerPartition) + "\nself " + h(InstalledPath) + " " + h(brokerReq) + " " + h(brokerPartition),
		"self " + h(InstalledPath) + " " + h(brokerReq) + " " + h(brokerPartition) + "\napp " + h(InstalledPath) + " " + h(brokerReq),
		"self " + h(InstalledPath) + " " + h(brokerReq) + " " + h(brokerPartition) + "\nacl zz",
		"self " + h(InstalledPath) + " " + h(brokerReq) + " " + h(brokerPartition) + "\nacl " + h("decrypt") + "\nany extra",
		"self " + h(InstalledPath) + " " + h(brokerReq) + " " + h(brokerPartition) + "\nacl " + h("decrypt") + "\nowner " + h("x"),
		"self " + h(InstalledPath) + " " + h(brokerReq) + " " + h(brokerPartition) + "\nacl " + h("decrypt") + "\napp " + h("a") + " " + h("b") + " " + h("c"),
	} {
		if _, err := parseAccess([]byte(bad)); err == nil {
			t.Fatalf("%q accepted", bad)
		}
	}
}

func TestReady(t *testing.T) {
	for _, account := range []string{"default", "work"} {
		login := ClaudeLogin(account)
		for _, tc := range []struct {
			err     error
			access  *AppAccess
			code    protocol.Code
			message string
		}{
			{nil, nil, "", ""},
			{ErrNotFound, nil, protocol.LoginRequired, "No Claude setup-token for account " + account + ": run " + login + "."},
			{errors.New("user denied"), nil, protocol.KeychainFailed, "Keychain access to the Claude account " + account + " setup-token failed."},
			{nil, reinstalled(), protocol.LoginRequired, "The Claude account " + account + " setup-token trusts another broker build: run " + login + "."},
			{nil, halfDone(), protocol.LoginRequired, "The Claude account " + account + " setup-token trusts another broker build: run " + login + "."},
		} {
			k := &fakeKeychain{account: account, fakeApps: fakeApps{access: tc.access, accessErr: tc.err}}
			err := Credentials{Keychain: k}.Ready(account)
			var e *protocol.Error
			if tc.code == "" && err != nil || tc.code != "" && (!errors.As(err, &e) || e.Code != tc.code || e.Message != tc.message) {
				t.Fatalf("%s: %v → %v", account, tc.err, err)
			}
			if k.gets != 0 || k.reads != 1 {
				t.Fatalf("%s: read the token %d, access %d", account, k.gets, k.reads)
			}
			if e != nil && e.Cause != "" {
				t.Fatalf("keychain detail in cause: %q", e.Cause)
			}
		}
	}
	// Only the selected account's item is checked.
	k := accounts{"default": {fakeApps: fakeApps{accessErr: errors.New("default must not be read")}}, "work": {account: "work"}}
	if err := (Credentials{Keychain: k}).Ready("work"); err != nil || k["work"].reads != 1 || k["default"].reads != 0 {
		t.Fatalf("%v, work %d, default %d", err, k["work"].reads, k["default"].reads)
	}
	var e *protocol.Error
	if err := (Credentials{Keychain: k}).Ready("other"); !errors.As(err, &e) || e.Code != protocol.LoginRequired ||
		e.Message != "No Claude setup-token for account other: run ensemble-broker login claude other." {
		t.Fatalf("other: %v", err)
	}
}

func TestInject(t *testing.T) {
	e := &fakeExec{}
	k := &fakeKeychain{token: []byte(token)}
	if err := (Credentials{Keychain: k}).Inject("default", e.run); err != nil {
		t.Fatal(err)
	}
	script := "mkdir -p -m 0700 " + docker.SecretsDir + " && umask 0377 && set -C && cat > " + docker.SecretsDir + `/"$1"`
	want := []string{"/bin/sh", "-c", script, "sh", "claude"}
	if !reflect.DeepEqual(e.argv, [][]string{want}) || string(e.stdin[0]) != token {
		t.Fatalf("%q %q", e.argv, e.stdin)
	}
	for _, tc := range []struct {
		dockerErr, keyErr error
		unreachable       bool
	}{
		{&docker.CommandError{Exit: 1, Stderr: "cat: " + token}, nil, false},
		{errors.Join(docker.ErrUnreachable, errors.New(token)), nil, true},
	} {
		e := &fakeExec{err: tc.dockerErr}
		err := Credentials{Keychain: &fakeKeychain{token: []byte(token), getErr: tc.keyErr}}.Inject("default", e.run)
		if err == nil || strings.Contains(err.Error(), token) || errors.Is(err, docker.ErrUnreachable) != tc.unreachable {
			t.Fatalf("%v", err)
		}
		var ce *docker.CommandError
		if errors.As(err, &ce) {
			t.Fatal("Docker stderr kept")
		}
	}
}

// Only the selected account's token enters the container: once, through exec's stdin; the other item is never read.
func TestInjectSelectedAccount(t *testing.T) {
	const workToken = "sk-ant-oat01-WORK"
	for selected, want := range map[string]string{"work": workToken, "default": token} {
		k := accounts{"default": {token: []byte(token)}, "work": {account: "work", token: []byte(workToken)}}
		e := &fakeExec{}
		if err := (Credentials{Keychain: k}).Inject(selected, e.run); err != nil {
			t.Fatal(err)
		}
		other := map[string]string{"work": "default", "default": "work"}[selected]
		if len(e.stdin) != 1 || string(e.stdin[0]) != want || k[selected].gets != 1 || k[other].gets != 0 || k[other].dialogGets != 0 || k[other].reads != 0 {
			t.Fatalf("%s: stdin %d, reads %d/%d", selected, len(e.stdin), k[selected].gets, k[other].gets)
		}
		if err := k[selected].erased(); err != nil {
			t.Fatal(err)
		}
	}
}

func TestInjectKeychainFailure(t *testing.T) {
	for _, account := range []string{"default", "work"} {
		for keyErr, message := range map[error]string{
			errors.New("locked " + token): "Keychain access to the Claude account " + account + " setup-token failed.",
			ErrNotFound:                   "No Claude setup-token for account " + account + ": run " + ClaudeLogin(account) + ".",
			ErrNeedsDialog:                "The Claude account " + account + " setup-token needs keychain approval: unlock the login keychain, or run " + ClaudeLogin(account) + ".",
		} {
			code := map[bool]protocol.Code{true: protocol.LoginRequired, false: protocol.KeychainFailed}[keyErr == ErrNotFound || keyErr == ErrNeedsDialog]
			e := &fakeExec{}
			err := Credentials{Keychain: &fakeKeychain{account: account, getErr: keyErr}}.Inject(account, e.run)
			var pe *protocol.Error
			if !errors.As(err, &pe) || pe.Code != code || pe.Cause != "" || pe.Message != message || strings.Contains(err.Error(), token) || len(e.argv) != 0 {
				t.Fatalf("%s %v: %v %q", account, keyErr, err, e.argv)
			}
		}
	}
}

// TestKeychainServices: KeychainServices lists exactly the package's ...Service string constants and, each ending in
// "/", its ...ServicePrefix ones (prefix items), so a new service cannot miss docs/credentials.md.
func TestKeychainServices(t *testing.T) {
	services, prefixes := serviceConstants(t)
	if len(services) < 5 || len(prefixes) < 1 {
		t.Fatalf("found %d service and %d prefix constants, want at least 5 and 1", len(services), len(prefixes))
	}
	if !prefixes[envServicePrefix] {
		t.Errorf("no prefix constant for %s", envServicePrefix)
	}
	for _, err := range checkServices(KeychainServices, services, prefixes) {
		t.Error(err)
	}
}

// serviceConstants are the package's string constants named ...Service and ...ServicePrefix.
func serviceConstants(t *testing.T) (services, prefixes map[string]bool) {
	t.Helper()
	files, err := filepath.Glob("*.go")
	if err != nil {
		t.Fatal(err)
	}
	services, prefixes = map[string]bool{}, map[string]bool{}
	fset := gotoken.NewFileSet()
	for _, name := range files {
		if strings.HasSuffix(name, "_test.go") {
			continue
		}
		file, err := parser.ParseFile(fset, name, nil, 0)
		if err != nil {
			t.Fatal(err)
		}
		for _, decl := range file.Decls {
			gen, ok := decl.(*ast.GenDecl)
			if !ok || gen.Tok != gotoken.CONST {
				continue
			}
			for _, spec := range gen.Specs {
				value := spec.(*ast.ValueSpec)
				for i, id := range value.Names {
					into := services
					if strings.HasSuffix(id.Name, "ServicePrefix") {
						into = prefixes
					} else if !strings.HasSuffix(id.Name, "Service") {
						continue
					}
					if i >= len(value.Values) {
						continue
					}
					lit, ok := value.Values[i].(*ast.BasicLit)
					if !ok || lit.Kind != gotoken.STRING {
						continue
					}
					service, err := strconv.Unquote(lit.Value)
					if err != nil {
						t.Fatal(err)
					}
					into[service] = true
				}
			}
		}
	}
	return services, prefixes
}

// checkServices is why list is not exactly services and prefixes: a prefix ends in "/", a service does not and
// starts with no listed prefix, each is listed once.
func checkServices(list []string, services, prefixes map[string]bool) []error {
	var errs []error
	listed := map[string]bool{}
	for _, service := range list {
		if listed[service] {
			errs = append(errs, fmt.Errorf("KeychainServices lists %s twice", service))
		}
		listed[service] = true
		switch {
		case prefixes[service] && !strings.HasSuffix(service, "/"):
			errs = append(errs, fmt.Errorf("prefix item %s does not end in /", service))
		case services[service] && strings.HasSuffix(service, "/"):
			errs = append(errs, fmt.Errorf("service %s ends in /: declare it as a ...ServicePrefix constant", service))
		case !services[service] && !prefixes[service]:
			errs = append(errs, fmt.Errorf("KeychainServices lists %s, neither a service nor a prefix constant", service))
		}
	}
	for _, service := range slices.Sorted(maps.Keys(services)) {
		if !listed[service] {
			errs = append(errs, fmt.Errorf("KeychainServices misses %s", service))
		}
		for prefix := range prefixes {
			if listed[prefix] && strings.HasPrefix(service, prefix) {
				errs = append(errs, fmt.Errorf("service %s starts with prefix item %s", service, prefix))
			}
		}
	}
	for _, prefix := range slices.Sorted(maps.Keys(prefixes)) {
		if !listed[prefix] {
			errs = append(errs, fmt.Errorf("KeychainServices misses prefix item %s", prefix))
		}
	}
	return errs
}

// TestCheckServices: each way a list can disagree with the constants is reported.
func TestCheckServices(t *testing.T) {
	services := map[string]bool{"ensemble-broker-claude": true, "ensemble-broker-bot": true}
	prefixes := map[string]bool{"ensemble-broker-env/": true}
	if errs := checkServices([]string{"ensemble-broker-claude", "ensemble-broker-bot", "ensemble-broker-env/"}, services, prefixes); len(errs) != 0 {
		t.Fatal(errs)
	}
	for name, tc := range map[string]struct {
		list               []string
		services, prefixes map[string]bool
		want               string
	}{
		"prefix missing":       {[]string{"ensemble-broker-claude", "ensemble-broker-bot"}, services, prefixes, "misses prefix item ensemble-broker-env/"},
		"prefix not constant":  {[]string{"ensemble-broker-claude", "ensemble-broker-bot", "ensemble-broker-env/", "other/"}, services, prefixes, "lists other/, neither"},
		"prefix without slash": {[]string{"ensemble-broker-claude", "ensemble-broker-bot", "ensemble-broker-env"}, services, map[string]bool{"ensemble-broker-env": true}, "does not end in /"},
		"service with slash":   {[]string{"ensemble-broker-claude", "ensemble-broker-bot", "x/", "ensemble-broker-env/"}, map[string]bool{"ensemble-broker-claude": true, "ensemble-broker-bot": true, "x/": true}, prefixes, "service x/ ends in /"},
		"service under prefix": {[]string{"ensemble-broker-claude", "ensemble-broker-env/x", "ensemble-broker-env/"}, map[string]bool{"ensemble-broker-claude": true, "ensemble-broker-env/x": true}, prefixes, "starts with prefix item ensemble-broker-env/"},
		"service missing":      {[]string{"ensemble-broker-claude", "ensemble-broker-env/"}, services, prefixes, "misses ensemble-broker-bot"},
		"twice":                {[]string{"ensemble-broker-claude", "ensemble-broker-bot", "ensemble-broker-env/", "ensemble-broker-env/"}, services, prefixes, "lists ensemble-broker-env/ twice"},
	} {
		errs := checkServices(tc.list, tc.services, tc.prefixes)
		if !strings.Contains(fmt.Sprint(errs), tc.want) {
			t.Errorf("%s: %v, want %q", name, errs, tc.want)
		}
	}
}

type accessOf func(service, account string) (AppAccess, error)

func (f accessOf) AppAccess(service, account string) (AppAccess, error) { return f(service, account) }

func TestTrustedItem(t *testing.T) {
	for name, tc := range map[string]struct {
		access  AppAccess
		err     error
		trusted bool
	}{
		"broker only":  {brokerOnly(), nil, true},
		"other app":    {*added(), nil, false},
		"absent":       {AppAccess{}, ErrNotFound, false},
		"read refused": {AppAccess{}, ErrNeedsDialog, false},
	} {
		var asked []string
		trusted, err := trustedItem(accessOf(func(service, account string) (AppAccess, error) {
			asked = append(asked, service+"/"+account)
			return tc.access, tc.err
		}), "ensemble-broker-env/x", "x")
		if trusted != tc.trusted || !errors.Is(err, tc.err) || !slices.Equal(asked, []string{"ensemble-broker-env/x/x"}) {
			t.Errorf("%s: %v %v %q", name, trusted, err, asked)
		}
	}
}

// TestStaticSecretErrorsHideKeychainDetail puts a sentinel in every Keychain error a static secret's start path can
// get; no error it returns names it.
func TestStaticSecretErrorsHideKeychainDetail(t *testing.T) {
	const sentinel = "KEYCHAIN_SENTINEL"
	leak := errors.New("keychain status -25293: " + sentinel)
	exec := (&fakeExec{}).run
	var errs []error
	claude := &fakeKeychain{token: []byte(token)}
	claude.accessErr = leak
	errs = append(errs, Credentials{Keychain: claude}.Ready("default"))
	errs = append(errs, Credentials{Keychain: &fakeKeychain{token: []byte(token), getErr: leak}}.Inject("default", exec))
	keys := providerKeys("strata")
	keys.accessErr = leak
	errs = append(errs, OpencodeKeys{Keychain: keys}.Ready([]string{"strata"}))
	keys = providerKeys("strata")
	keys.readErr = leak
	errs = append(errs, OpencodeKeys{Keychain: keys}.Inject([]string{"strata"}, exec))
	env := envKeys("dummy")
	env.accessErr = leak
	errs = append(errs, EnvKeys{Keychain: env}.Ready([]string{"ensemble-broker-env/dummy"}))
	env = envKeys("dummy")
	env.readErr = leak
	errs = append(errs, EnvKeys{Keychain: env}.Inject(launcher.Secret{Item: "ensemble-broker-env/dummy", File: "dummy.txt"}, exec))
	for i, err := range errs {
		if err == nil || strings.Contains(err.Error(), sentinel) || errors.Is(err, leak) {
			t.Errorf("%d: %v", i, err)
		}
	}
}

// TestStaticPrefixesMatchServices: each static secret's reference is its keychain service and account, and its login
// command is the one this package names.
func TestStaticPrefixesMatchServices(t *testing.T) {
	if credential.ClaudePrefix != tokenService+"/" || credential.OpencodePrefix != opencodeService+"/" || credential.EnvPrefix != envServicePrefix {
		t.Fatal("credential's static prefixes disagree with the keychain services")
	}
	for _, account := range []string{"default", "work"} {
		if ClaudeLogin(account) != credential.LoginCommand(credential.StaticRef(credential.Claude, account)) {
			t.Fatalf("%s: %q", account, ClaudeLogin(account))
		}
	}
	if opencodeLogin("strata") != "ensemble-broker login opencode strata" || envLogin("db") != "ensemble-broker login env db" {
		t.Fatal("login commands")
	}
}
