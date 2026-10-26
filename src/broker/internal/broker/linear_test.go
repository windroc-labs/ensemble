package broker

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"ensemble/broker/internal/audit"
	"ensemble/broker/internal/credential"
)

var linearNow = time.Date(2026, 10, 26, 12, 0, 0, 0, time.UTC)

func linearApp(name string) LinearIdentity {
	return LinearIdentity{Name: name, Kind: "role", ClientID: "client-" + name,
		UserID: "00000000-0000-4000-8000-000000000001", SecretService: "root-" + name, TokenService: "token-" + name}
}

// All Keychain operations in this file terminate in this persistent fake, including ACL changes.
type linearKeysFake struct {
	mu sync.Mutex
	fakeApps
	items          map[string][]byte
	fail           string
	failWriteKey   string
	destructive    bool // models the existing native PutSecret's delete-before-add failure
	untrusted      map[string]bool
	metadataAccess map[string]AppAccess
}

func (k *linearKeysFake) Secret(s, a string) ([]byte, error) {
	k.mu.Lock()
	defer k.mu.Unlock()
	if k.fail == s {
		return nil, errors.New("ROOT-SENTINEL")
	}
	v, ok := k.items[s+"/"+a]
	if !ok {
		return nil, ErrNotFound
	}
	return bytes.Clone(v), nil
}
func (k *linearKeysFake) PutSecret(s, a string, v []byte) error {
	k.mu.Lock()
	defer k.mu.Unlock()
	if k.failWriteKey == s+"/"+a {
		if k.destructive {
			delete(k.items, s+"/"+a)
		}
		return errors.New("ROOT-SENTINEL")
	}
	if k.fail == s {
		return errors.New("ROOT-SENTINEL")
	}
	k.items[s+"/"+a] = bytes.Clone(v)
	return nil
}
func (k *linearKeysFake) DeleteSecret(s, a string) error {
	k.mu.Lock()
	defer k.mu.Unlock()
	delete(k.items, s+"/"+a)
	return nil
}
func (k *linearKeysFake) AppAccount(s string) (string, error) { return s, nil }
func (k *linearKeysFake) AppAccess(s, a string) (AppAccess, error) {
	k.mu.Lock()
	defer k.mu.Unlock()
	if s == linearService || s == linearGrantsService {
		if _, ok := k.items[s+"/"+a]; !ok {
			return AppAccess{}, ErrNotFound
		}
		if k.untrusted[s+"/"+a] {
			return *added(), nil
		}
		if access, ok := k.metadataAccess[s+"/"+a]; ok {
			copy := access
			copy.Entries = slices.Clone(access.Entries)
			for i, e := range copy.Entries {
				copy.Entries[i].Apps = slices.Clone(e.Apps)
				copy.Entries[i].Partitions = slices.Clone(e.Partitions)
				copy.Entries[i].Authorizations = slices.Clone(e.Authorizations)
			}
			return copy, nil
		}
		return brokerOnly(), nil
	}
	return k.fakeApps.AppAccess(s, a)
}

func (k *linearKeysFake) UpdateSecret(s, a string, v []byte) error {
	k.mu.Lock()
	defer k.mu.Unlock()
	if k.fail == s || k.failWriteKey == s+"/"+a {
		return errors.New("ROOT-SENTINEL")
	}
	k.items[s+"/"+a] = bytes.Clone(v)
	return nil
}

func (k *linearKeysFake) repairMetadata(s, a string, partition bool) error {
	k.mu.Lock()
	defer k.mu.Unlock()
	if k.frozen {
		return nil
	}
	if k.metadataAccess == nil {
		k.metadataAccess = map[string]AppAccess{}
	}
	access, ok := k.metadataAccess[s+"/"+a]
	if !ok {
		access = brokerOnly()
	}
	for i, e := range access.Entries {
		if partition && slices.Contains(e.Authorizations, "partition_id") {
			access.Entries[i].Partitions = []string{brokerPartition}
		}
		if !partition && slices.ContainsFunc(e.Authorizations, func(auth string) bool { return slices.Contains(readAuthorizations, auth) }) {
			access.Entries[i].Any = false
			access.Entries[i].Apps = []string{brokerApp}
		}
	}
	k.metadataAccess[s+"/"+a] = access
	return nil
}
func (k *linearKeysFake) TrustApps(s, a string, password []byte) error {
	if s == linearService || s == linearGrantsService {
		return k.repairMetadata(s, a, false)
	}
	k.mu.Lock()
	defer k.mu.Unlock()
	return k.fakeApps.TrustApps(s, a, password)
}
func (k *linearKeysFake) TrustPartition(s, a string, password []byte) error {
	if s == linearService || s == linearGrantsService {
		return k.repairMetadata(s, a, true)
	}
	k.mu.Lock()
	defer k.mu.Unlock()
	return k.fakeApps.TrustPartition(s, a, password)
}
func (k *linearKeysFake) TrustAppsByDialog(s, a string) error {
	if s == linearService || s == linearGrantsService {
		return k.repairMetadata(s, a, false)
	}
	k.mu.Lock()
	defer k.mu.Unlock()
	return k.fakeApps.TrustAppsByDialog(s, a)
}

type linearHostFake struct {
	mu     sync.Mutex
	values map[[2]string][]byte
	fail   bool
	writes int
}

func (h *linearHostFake) Read(service, account string) ([]byte, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	value, ok := h.values[[2]string{service, account}]
	if !ok {
		return nil, ErrNotFound
	}
	return bytes.Clone(value), nil
}
func (h *linearHostFake) Publish(_ context.Context, service, account string, v []byte) error {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.writes++
	if h.fail {
		return errors.New("TOKEN-SENTINEL")
	}
	h.values[[2]string{service, account}] = bytes.Clone(v)
	return nil
}

func (h *linearHostFake) value(t *testing.T, a LinearIdentity) []byte {
	t.Helper()
	value, err := h.Read(a.TokenService, a.TokenService) // fake AppAccount returns the service
	if err != nil {
		t.Fatal(err)
	}
	return value
}

type linearRoundTrip func(*http.Request) (*http.Response, error)

func (f linearRoundTrip) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func linearRig(t *testing.T) (Linear, *linearKeysFake, *linearHostFake, *[]url.Values) {
	t.Helper()
	k := &linearKeysFake{items: map[string][]byte{}}
	h := &linearHostFake{values: map[[2]string][]byte{}}
	var calls []url.Values
	var callsMu sync.Mutex
	l := NewLinear(t.TempDir(), nil)
	l.keys, l.host, l.now = k, h, func() time.Time { return linearNow }
	l.http = &http.Client{Transport: linearRoundTrip(func(r *http.Request) (*http.Response, error) {
		b, _ := io.ReadAll(r.Body)
		v, _ := url.ParseQuery(string(b))
		callsMu.Lock()
		calls = append(calls, v)
		n := len(calls)
		callsMu.Unlock()
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(fmt.Sprintf(`{"access_token":"TOKEN-%d","expires_in":2592000}`, n)))}, nil
	})}
	return l, k, h, &calls
}

func enrollLinear(t *testing.T, l Linear, k *linearKeysFake, app LinearIdentity) {
	t.Helper()
	k.PutSecret(app.SecretService, app.SecretService, []byte("ROOT-SENTINEL"))
	if err := l.Enroll(app); err != nil {
		t.Fatal(err)
	}
}

func TestLinearIdentityInput(t *testing.T) {
	a := linearApp("verifier")
	b, _ := json.Marshal(a)
	if got, err := ReadLinearIdentity(bytes.NewReader(b)); err != nil || got != a {
		t.Fatalf("%+v %v", got, err)
	}
	for _, text := range []string{
		string(b) + ` {}`, strings.Replace(string(b), `"verifier"`, `"bad\nname"`, 1),
		strings.Replace(string(b), `"role"`, `"admin"`, 1),
		strings.Replace(string(b), `"client_id":`, `"extra":1,"client_id":`, 1),
		strings.Replace(string(b), `"name":`, `"name":"other","name":`, 1),
		strings.Repeat(" ", 8193), `{}`, `null`,
	} {
		if _, err := ReadLinearIdentity(strings.NewReader(text)); err == nil {
			t.Fatalf("accepted invalid input")
		}
	}
	for _, s := range []string{"ensemble-codex", "ensemble-env/x", ".", "..", "bad\ncommand"} {
		a.TokenService = s
		if a.check() == nil {
			t.Errorf("accepted service %q", s)
		}
	}
}

func TestLinearHostRenewalAndBoundedGrant(t *testing.T) {
	for kind, expectedScope := range map[string]string{"harness": "read,write", "manager": "read,write,app:assignable", "role": "read,write,app:assignable,app:mentionable"} {
		t.Run(kind, func(t *testing.T) {
			l, k, h, calls := linearRig(t)
			a := linearApp("verifier")
			a.Kind = kind
			enrollLinear(t, l, k, a)
			for i := range 3 {
				clock := linearNow.Add(time.Duration(i) * 30 * 24 * time.Hour)
				l.now = func() time.Time { return clock }
				got, err := l.Maintain(context.Background(), a)
				if err != nil || !got.Renewed {
					t.Fatalf("%+v %v", got, err)
				}
				if got, err = l.Maintain(context.Background(), a); err != nil || got.Renewed {
					t.Fatalf("repeated %+v %v", got, err)
				}
			}
			if len(*calls) != 3 {
				t.Fatalf("requests %d", len(*calls))
			}
			for _, form := range *calls {
				if form.Get("scope") != expectedScope || form.Get("client_secret") != "ROOT-SENTINEL" || form.Get("client_id") != a.ClientID {
					t.Fatal("wrong identity/scope")
				}
			}
			var token map[string]any
			json.Unmarshal(h.value(t, a), &token)
			if len(token) != 2 || token["access_token"] != "TOKEN-3" {
				t.Fatalf("publication keys %v", token)
			}
			data, _ := k.Secret(linearGrantsService, "host/"+a.TokenService)
			var rec credential.Record
			json.Unmarshal(data, &rec)
			if len(rec.Tokens) != 1 || rec.Tokens[0].State != credential.Delivered {
				t.Fatalf("grant %+v", rec)
			}
		})
	}
}

func TestLinearReconstructionRepairsPublicationWithoutRenewal(t *testing.T) {
	l, k, h, calls := linearRig(t)
	a := linearApp("verifier")
	enrollLinear(t, l, k, a)
	h.fail = true
	if _, err := l.Maintain(context.Background(), a); err == nil || strings.Contains(err.Error(), "SENTINEL") {
		t.Fatalf("failure %v", err)
	}
	h.fail = false
	rebuilt := NewLinear(t.TempDir(), nil)
	rebuilt.keys, rebuilt.host, rebuilt.http, rebuilt.now = k, h, l.http, l.now
	if got, err := rebuilt.Maintain(context.Background(), a); err != nil || got.Renewed || len(*calls) != 1 {
		t.Fatalf("repair %+v %v, requests %d", got, err, len(*calls))
	}
	var published linearAccess
	if json.Unmarshal(h.value(t, a), &published) != nil || published.AccessToken != "TOKEN-1" || published.ExpiresAt != "2026-11-25T12:00:00Z" {
		t.Fatalf("unrepaired publication %+v", published)
	}
}

func TestLinearLegacyExpiryIsCanonical(t *testing.T) {
	l, k, h, calls := linearRig(t)
	a := linearApp("harness")
	enrollLinear(t, l, k, a)
	h.values[[2]string{a.TokenService, a.TokenService}] = []byte(`{"access_token":"OLD","expires_at":"2026-11-25T12:00:00+00:00"}`)
	got, err := l.Maintain(context.Background(), a)
	if err != nil || got.ExpiresAt != "2026-11-25T12:00:00Z" || got.Renewed || len(*calls) != 0 {
		t.Fatalf("legacy reply %+v %v", got, err)
	}
}

func TestLinearRegistryUpdateFailureKeepsExistingReservations(t *testing.T) {
	l, k, _, _ := linearRig(t)
	a := linearApp("harness")
	enrollLinear(t, l, k, a)
	b := linearApp("verifier")
	b.UserID = "00000000-0000-4000-8000-000000000002"
	k.failWriteKey = linearService + "/registry"
	k.destructive = true
	if err := l.Enroll(b); err == nil {
		t.Fatal("failed registry update accepted")
	}
	k.failWriteKey = ""
	var registry []LinearIdentity
	if err := l.read("registry", &registry); err != nil || len(registry) != 1 || registry[0] != a {
		t.Fatalf("lost old reservation: %v %+v", err, registry)
	}
	rebuilt := NewLinear(t.TempDir(), nil)
	rebuilt.keys = k
	alias := a
	alias.Name = "alias"
	alias.TokenService = "token-alias"
	if err := rebuilt.Enroll(alias); err == nil {
		t.Fatal("same identity received two account locks")
	}
}

func TestLinearRejectsPlantedMetadataAndUnregisteredBinding(t *testing.T) {
	for _, attack := range []string{"account trust", "registry trust", "missing registry", "binding"} {
		t.Run(attack, func(t *testing.T) {
			l, k, h, calls := linearRig(t)
			a := linearApp("harness")
			enrollLinear(t, l, k, a)
			k.untrusted = map[string]bool{}
			switch attack {
			case "account trust":
				k.untrusted[linearService+"/app/"+a.TokenService] = true
			case "registry trust":
				k.untrusted[linearService+"/registry"] = true
			case "missing registry":
				k.DeleteSecret(linearService, "registry")
			case "binding":
				alias := a
				alias.Name = "verifier"
				alias.Kind = "role"
				alias.TokenService = "token-forged"
				value, _ := json.Marshal(linearAccount{Version: 1, Identity: alias, RootAccount: a.SecretService, TokenAccount: alias.TokenService, Active: true})
				k.PutSecret(linearService, "app/"+alias.TokenService, value)
				a = alias
			}
			if _, err := l.Maintain(context.Background(), a); err == nil || len(*calls) != 0 || h.writes != 0 {
				t.Fatal("planted state authorized root use or publication")
			}
		})
	}
}

func TestLinearPendingEnrollmentReconstruction(t *testing.T) {
	l, k, h, calls := linearRig(t)
	a := linearApp("verifier")
	k.PutSecret(a.SecretService, a.SecretService, []byte("ROOT-SENTINEL"))
	k.access = added()
	k.frozen = true
	l.password = func() ([]byte, error) { return []byte(keychainPass), nil }
	if err := l.Enroll(a); err == nil {
		t.Fatal("failed restriction accepted")
	}
	if _, err := l.Maintain(context.Background(), a); err == nil || len(*calls) != 0 || h.writes != 0 {
		t.Fatal("pending enrollment active")
	}
	k.frozen = false
	rebuilt := NewLinear(t.TempDir(), l.password)
	rebuilt.keys, rebuilt.host, rebuilt.http, rebuilt.now = k, h, l.http, l.now
	if err := rebuilt.Enroll(a); err != nil {
		t.Fatal(err)
	}
	if onlyBroker(*k.access) != nil {
		t.Fatal("root restriction not repaired")
	}
	if _, err := rebuilt.Maintain(context.Background(), a); err != nil || len(*calls) != 1 {
		t.Fatalf("reconstructed enrollment %v", err)
	}
}

func TestLinearAccountSaveFailureNeverPublishes(t *testing.T) {
	l, k, h, calls := linearRig(t)
	a := linearApp("verifier")
	enrollLinear(t, l, k, a)
	k.failWriteKey = linearService + "/app/" + a.TokenService
	if _, err := l.Maintain(context.Background(), a); err == nil || len(*calls) != 1 || h.writes != 0 {
		t.Fatal("published before durable access-token save")
	}
	k.failWriteKey = ""
	var account linearAccount
	if err := l.read("app/"+a.TokenService, &account); err != nil || !account.Active || account.Current.AccessToken != "" {
		t.Fatalf("lost old enrollment %+v %v", account, err)
	}
}

func TestLinearBlockedIdentityDoesNotBlockAnotherRenewal(t *testing.T) {
	l, k, h, _ := linearRig(t)
	a, b := linearApp("harness"), linearApp("verifier")
	b.UserID = "00000000-0000-4000-8000-000000000002"
	enrollLinear(t, l, k, a)
	enrollLinear(t, l, k, b)
	entered, release := make(chan struct{}), make(chan struct{})
	l.http = &http.Client{Transport: linearRoundTrip(func(r *http.Request) (*http.Response, error) {
		data, _ := io.ReadAll(r.Body)
		form, _ := url.ParseQuery(string(data))
		if form.Get("client_id") == a.ClientID {
			close(entered)
			<-release
		}
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(fmt.Sprintf(`{"access_token":"NEW-%s","expires_in":2592000}`, form.Get("client_id"))))}, nil
	})}
	resultA, resultB := make(chan error, 1), make(chan error, 1)
	go func() { _, err := l.Maintain(context.Background(), a); resultA <- err }()
	<-entered
	go func() { _, err := l.Maintain(context.Background(), b); resultB <- err }()
	completedB := false
	select {
	case err := <-resultB:
		completedB = true
		if err != nil {
			t.Error(err)
		}
	case <-time.After(time.Second):
		t.Error("unrelated identity blocked")
	}
	close(release)
	if err := <-resultA; err != nil {
		t.Error(err)
	}
	if !completedB {
		if err := <-resultB; err != nil {
			t.Error(err)
		}
	}
	if h.writes != 2 {
		t.Fatalf("publications %d", h.writes)
	}
	for _, identity := range []LinearIdentity{a, b} {
		var published linearAccess
		if err := json.Unmarshal(h.value(t, identity), &published); err != nil || published.AccessToken != "NEW-"+identity.ClientID {
			t.Fatalf("wrong identity publication: %+v %v", published, err)
		}
	}
}

func TestLinearIdentityPublicationsSurviveRenewalAndReconstruction(t *testing.T) {
	l, k, h, calls := linearRig(t)
	a, b := linearApp("harness"), linearApp("verifier")
	a.Kind = "harness"
	b.UserID = "00000000-0000-4000-8000-000000000002"
	for _, identity := range []LinearIdentity{a, b} {
		enrollLinear(t, l, k, identity)
		if _, err := l.Maintain(context.Background(), identity); err != nil {
			t.Fatal(err)
		}
	}
	check := func(identity LinearIdentity, token, expiry string) []byte {
		t.Helper()
		value := h.value(t, identity)
		var published linearAccess
		if err := json.Unmarshal(value, &published); err != nil || published.AccessToken != token || published.ExpiresAt != expiry {
			t.Fatalf("wrong publication for %s: %+v %v", identity.Name, published, err)
		}
		return value
	}
	check(a, "TOKEN-1", "2026-11-25T12:00:00Z")
	oldB := check(b, "TOKEN-2", "2026-11-25T12:00:00Z")
	l.now = func() time.Time { return linearNow.Add(26 * 24 * time.Hour) }
	if got, err := l.Maintain(context.Background(), a); err != nil || !got.Renewed {
		t.Fatalf("renewal: %+v %v", got, err)
	}
	newA := check(a, "TOKEN-3", "2026-12-21T12:00:00Z")
	if !bytes.Equal(h.value(t, b), oldB) {
		t.Fatal("harness renewal changed verifier publication")
	}
	h.fail = true
	if _, err := l.Maintain(context.Background(), b); err == nil {
		t.Fatal("failed verifier publication accepted")
	}
	h.fail = false
	if !bytes.Equal(h.value(t, a), newA) || !bytes.Equal(h.value(t, b), oldB) || len(*calls) != 4 {
		t.Fatal("failed publication changed either host entry or did not renew verifier")
	}
	rebuilt := NewLinear(t.TempDir(), nil)
	rebuilt.keys, rebuilt.host, rebuilt.http, rebuilt.now = k, h, l.http, l.now
	if got, err := rebuilt.Maintain(context.Background(), b); err != nil || got.Renewed || len(*calls) != 4 {
		t.Fatalf("verifier repair: %+v %v, requests %d", got, err, len(*calls))
	}
	newB := check(b, "TOKEN-4", "2026-12-21T12:00:00Z")
	if !bytes.Equal(h.value(t, a), newA) {
		t.Fatal("verifier repair changed harness publication")
	}
	if got, err := rebuilt.Maintain(context.Background(), a); err != nil || got.Renewed || len(*calls) != 4 {
		t.Fatalf("harness reconstruction: %+v %v, requests %d", got, err, len(*calls))
	}
	if !bytes.Equal(h.value(t, a), newA) || !bytes.Equal(h.value(t, b), newB) || len(h.values) != 2 {
		t.Fatal("reconstruction changed identity material or published to another service/account")
	}
}

func TestLinearEnrollmentWaitDoesNotHoldRegistry(t *testing.T) {
	l, k, _, _ := linearRig(t)
	a, b := linearApp("harness"), linearApp("verifier")
	b.UserID = "00000000-0000-4000-8000-000000000002"
	enrollLinear(t, l, k, a)
	k.PutSecret(b.SecretService, b.SecretService, []byte("ROOT-SENTINEL"))
	base := l.lock
	unlock, err := base("account/" + a.TokenService)()
	if err != nil {
		t.Fatal(err)
	}
	waiting := make(chan struct{})
	l.lock = func(id string) func() (func(), error) {
		return func() (func(), error) {
			if id == "account/"+a.TokenService {
				close(waiting)
			}
			return base(id)()
		}
	}
	resultA, resultB := make(chan error, 1), make(chan error, 1)
	go func() { resultA <- l.Enroll(a) }()
	<-waiting
	go func() { resultB <- l.Enroll(b) }()
	blocked := false
	select {
	case err := <-resultB:
		if err != nil {
			t.Error(err)
		}
	case <-time.After(time.Second):
		blocked = true
	}
	unlock()
	if err := <-resultA; err != nil {
		t.Error(err)
	}
	if blocked {
		if err := <-resultB; err != nil {
			t.Error(err)
		}
		t.Fatal("identity wait held the registry lock")
	}
}

func TestLinearPublicationRejectsUnconfirmedValueOrTrust(t *testing.T) {
	value := []byte(`{"access_token":"TOKEN","expires_at":"2026-11-26T12:00:00Z"}`)
	for _, failure := range []string{"mismatch", "read error", "any reader", "unexpected reader", "no reader"} {
		t.Run(failure, func(t *testing.T) {
			h := linearHostEntries{home: "/fake", run: func(_ context.Context, args []string, _ []byte) ([]byte, error) {
				if slices.Contains(args, "-i") {
					return nil, nil
				}
				if failure == "mismatch" {
					return []byte("other"), nil
				}
				if failure == "read error" {
					return nil, errors.New("SENTINEL")
				}
				return value, nil
			}, access: func(string, string) (AppAccess, error) {
				a := brokerOnly()
				a.Entries[1].Apps = append(a.Entries[1].Apps, security)
				switch failure {
				case "any reader":
					a.Entries[1].Any = true
				case "unexpected reader":
					a.Entries[1].Apps = append(a.Entries[1].Apps, "/evil")
				case "no reader":
					a.Entries[1].Apps = nil
				}
				return a, nil
			}}
			if err := h.Publish(context.Background(), "token-verifier", "token-verifier", value); err == nil || strings.Contains(err.Error(), "SENTINEL") {
				t.Fatalf("invalid confirmation %v", err)
			}
		})
	}
}

func TestLinearReenrollmentRepairsPreviousBrokerMetadata(t *testing.T) {
	l, k, h, calls := linearRig(t)
	a := linearApp("harness")
	enrollLinear(t, l, k, a)
	if _, err := l.Maintain(context.Background(), a); err != nil {
		t.Fatal(err)
	}
	before := map[string][]byte{}
	k.metadataAccess = map[string]AppAccess{}
	for _, entry := range [][2]string{{linearService, "registry"}, {linearService, "app/" + a.TokenService}, {linearGrantsService, "host/" + a.TokenService}} {
		key := entry[0] + "/" + entry[1]
		before[key] = bytes.Clone(k.items[key])
		k.metadataAccess[key] = *reinstalled()
	}
	k.access = reinstalled()
	if _, err := l.Maintain(context.Background(), a); err == nil {
		t.Fatal("stale broker metadata accepted")
	}
	passwordReads := 0
	rebuilt := NewLinear(t.TempDir(), func() ([]byte, error) { passwordReads++; return []byte(keychainPass), nil })
	rebuilt.keys, rebuilt.host, rebuilt.http, rebuilt.now = k, h, l.http, l.now
	k.frozen = true
	if err := rebuilt.Enroll(a); err == nil {
		t.Fatal("failed metadata restriction accepted")
	}
	for key, value := range before {
		if !bytes.Equal(value, k.items[key]) {
			t.Fatal("failed repair discarded metadata")
		}
	}
	k.frozen = false
	passwordReads = 0
	if err := rebuilt.Enroll(a); err != nil {
		t.Fatalf("reinstall enrollment: %v", err)
	}
	if passwordReads != 1 {
		t.Fatalf("password reads %d", passwordReads)
	}
	for key, value := range before {
		if !bytes.Equal(value, k.items[key]) {
			t.Fatalf("recovery changed metadata bytes for %s", key)
		}
	}
	if got, err := rebuilt.Maintain(context.Background(), a); err != nil || got.Renewed || len(*calls) != 1 {
		t.Fatalf("cached material lost after recovery %+v %v", got, err)
	}
}

func TestLinearMetadataRecoveryRequiresPriorBrokerOnly(t *testing.T) {
	if !previousBrokerOnly(*reinstalled()) {
		t.Fatal("prior broker identity rejected")
	}
	for _, variant := range []string{"any", "foreign", "mixed", "partition", "no decrypt"} {
		t.Run(variant, func(t *testing.T) {
			a := *reinstalled()
			switch variant {
			case "any":
				a.Entries[1].Any = true
			case "foreign":
				a.Entries[1].Apps = []string{"/foreign (signature)"}
			case "mixed":
				a.Entries[1].Apps = append(a.Entries[1].Apps, security)
			case "partition":
				a.Entries[2].Partitions = []string{"apple-tool:"}
			case "no decrypt":
				a.Entries[1].Authorizations = []string{"encrypt"}
			}
			if previousBrokerOnly(a) {
				t.Fatal("untrusted metadata accepted for repair")
			}
		})
	}
}

func TestLinearReenrollmentNeverAdoptsPermissiveMetadata(t *testing.T) {
	for _, where := range []string{"registry", "app", "grant"} {
		t.Run(where, func(t *testing.T) {
			l, k, _, _ := linearRig(t)
			a := linearApp("harness")
			enrollLinear(t, l, k, a)
			if _, err := l.Maintain(context.Background(), a); err != nil {
				t.Fatal(err)
			}
			k.untrusted = map[string]bool{}
			switch where {
			case "registry":
				k.untrusted[linearService+"/registry"] = true
			case "app":
				k.untrusted[linearService+"/app/"+a.TokenService] = true
			case "grant":
				k.untrusted[linearGrantsService+"/host/"+a.TokenService] = true
			}
			asked := false
			l.password = func() ([]byte, error) { asked = true; return []byte(keychainPass), nil }
			if err := l.Enroll(a); err == nil || asked {
				t.Fatal("permissive metadata adopted during recovery")
			}
		})
	}
}

func TestLinearOAuthFailuresKeepDurableCurrentMaterial(t *testing.T) {
	cases := map[string]string{"malformed": "{", "object": "[]", "missing": `{}`, "unclean": `{"access_token":"bad\nvalue","expires_in":60}`, "root echo": `{"access_token":"ROOT-SENTINEL","expires_in":60}`, "zero": `{"access_token":"NEW","expires_in":0}`, "negative": `{"access_token":"NEW","expires_in":-1}`, "boolean": `{"access_token":"NEW","expires_in":true}`, "fraction": `{"access_token":"NEW","expires_in":1.5}`, "huge": `{"access_token":"NEW","expires_in":999999999999}`, "oversized": strings.Repeat("x", 65537), "transport": "", "status": `{"access_token":"NEW","expires_in":60}`}
	for name, body := range cases {
		t.Run(name, func(t *testing.T) {
			l, k, h, _ := linearRig(t)
			a := linearApp("harness")
			enrollLinear(t, l, k, a)
			if _, err := l.Maintain(context.Background(), a); err != nil {
				t.Fatal(err)
			}
			before, _ := k.Secret(linearService, "app/"+a.TokenService)
			published := h.value(t, a)
			writes := h.writes
			l.now = func() time.Time { return linearNow.Add(26 * 24 * time.Hour) }
			l.http = &http.Client{Transport: linearRoundTrip(func(*http.Request) (*http.Response, error) {
				if name == "transport" {
					return nil, errors.New("ROOT-SENTINEL")
				}
				status := 200
				if name == "status" {
					status = 401
				}
				return &http.Response{StatusCode: status, Body: io.NopCloser(strings.NewReader(body))}, nil
			})}
			_, err := l.Maintain(context.Background(), a)
			if err == nil || strings.Contains(err.Error(), "SENTINEL") {
				t.Fatalf("invalid response accepted or leaked: %v", err)
			}
			after, _ := k.Secret(linearService, "app/"+a.TokenService)
			if !bytes.Equal(before, after) || !bytes.Equal(published, h.value(t, a)) || h.writes != writes {
				t.Fatal("failed OAuth replaced durable or published access material")
			}
		})
	}
}

func TestLinearMaintenanceAudit(t *testing.T) {
	for _, renew := range []bool{false, true} {
		name := "current"
		if renew {
			name = "renewing"
		}
		t.Run(name, func(t *testing.T) {
			l, k, _, calls := linearRig(t)
			a := linearApp("harness")
			enrollLinear(t, l, k, a)
			if _, err := l.Maintain(context.Background(), a); err != nil {
				t.Fatal(err)
			}
			if renew {
				l.now = func() time.Time { return linearNow.Add(26 * 24 * time.Hour) }
			}
			sink := &lines{}
			rec := &audit.Recorder{Command: "linear-maintain", Container: "host/" + a.TokenService, Log: sink, Now: l.now}
			l.Audit = rec
			result, err := l.Maintain(context.Background(), a)
			if err != nil || result.Renewed != renew {
				t.Fatalf("maintenance: %+v %v", result, err)
			}
			if err := rec.Flush(nil); err != nil {
				t.Fatal(err)
			}
			if !renew {
				if len(*calls) != 1 || len(sink.got) != 0 {
					t.Fatal("current token made a provider request or wrote an audit line")
				}
				return
			}
			if len(*calls) != 2 || len(sink.got) != 1 {
				t.Fatalf("renewal: %d provider requests, %d audit lines", len(*calls), len(sink.got))
			}
			var event struct {
				Command string `json:"command"`
				logLine
			}
			if err := json.Unmarshal(sink.got[0], &event); err != nil {
				t.Fatal(err)
			}
			if event.Command != "linear-maintain" || event.Event != "refresh" || event.Container != "host/"+a.TokenService {
				t.Fatalf("wrong audit routing: %+v", event)
			}
			renewed := 0
			for _, c := range event.Credentials {
				if c.Kind != audit.LinearAccessToken || c.Account != a.Name {
					t.Fatalf("wrong audit credential: %+v", c)
				}
				if c.Result == audit.Renewed {
					renewed++
					if c.ExpiresAt != result.ExpiresAt {
						t.Fatalf("audit expiry %q differs from renewed expiry %q", c.ExpiresAt, result.ExpiresAt)
					}
				}
			}
			if renewed != 1 {
				t.Fatalf("want one renewed credential, got %d", renewed)
			}
			if bytes.Contains(sink.got[0], []byte("ROOT-SENTINEL")) || bytes.Contains(sink.got[0], []byte("TOKEN-")) {
				t.Fatal("audit contains root or access material")
			}
		})
	}
}

func TestLinearRenewalBoundaries(t *testing.T) {
	for lifetime, renewAfter := range map[time.Duration]time.Duration{30 * 24 * time.Hour: 25 * 24 * time.Hour, time.Minute: 30 * time.Second} {
		t.Run(lifetime.String(), func(t *testing.T) {
			l, k, _, _ := linearRig(t)
			a := linearApp("harness")
			enrollLinear(t, l, k, a)
			clock := linearNow
			l.now = func() time.Time { return clock }
			requests := 0
			l.http = &http.Client{Transport: linearRoundTrip(func(*http.Request) (*http.Response, error) {
				requests++
				return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(fmt.Sprintf(`{"access_token":"TOKEN-%d","expires_in":%d}`, requests, int64(lifetime/time.Second))))}, nil
			})}
			if _, err := l.Maintain(context.Background(), a); err != nil {
				t.Fatal(err)
			}
			boundary := linearNow.Add(renewAfter)
			for _, at := range []time.Time{boundary.Add(-time.Nanosecond), boundary} {
				clock = at
				if got, err := l.Maintain(context.Background(), a); err != nil || got.Renewed || requests != 1 {
					t.Fatalf("renewed before strictly due %+v %v requests %d", got, err, requests)
				}
			}
			clock = boundary.Add(time.Nanosecond)
			if got, err := l.Maintain(context.Background(), a); err != nil || !got.Renewed || requests != 2 {
				t.Fatalf("did not renew after boundary %+v %v requests %d", got, err, requests)
			}
		})
	}
}

func TestLinearEnrollmentRejectsCollisionsAndMismatches(t *testing.T) {
	l, k, _, calls := linearRig(t)
	a := linearApp("harness")
	enrollLinear(t, l, k, a)
	if err := l.Enroll(a); err != nil {
		t.Fatal(err)
	}
	for _, field := range []string{"root", "token", "client", "user"} {
		b := linearApp("verifier")
		b.UserID = "00000000-0000-4000-8000-000000000002"
		switch field {
		case "root":
			b.TokenService = a.SecretService
		case "token":
			b.SecretService = a.TokenService
		case "client":
			b.ClientID = a.ClientID
		case "user":
			b.UserID = a.UserID
		}
		if err := l.Enroll(b); err == nil {
			t.Fatalf("accepted %s collision", field)
		}
	}
	a.ClientID = "other"
	if _, err := l.Maintain(context.Background(), a); err == nil || len(*calls) != 0 {
		t.Fatal("identity rebound")
	}
}

func TestLinearTrustAndStorageFailuresDoNotIssue(t *testing.T) {
	l, k, _, calls := linearRig(t)
	a := linearApp("verifier")
	enrollLinear(t, l, k, a)
	k.access = added()
	if _, err := l.Maintain(context.Background(), a); err == nil {
		t.Fatal("permissive root accepted")
	}
	access := brokerOnly()
	k.access = &access
	k.fail = linearService
	if _, err := l.Maintain(context.Background(), a); err == nil || strings.Contains(err.Error(), "SENTINEL") {
		t.Fatalf("unsafe error %v", err)
	}
	if len(*calls) != 0 {
		t.Fatal("issued before validated trust/state")
	}
}

func TestLinearConcurrentRenewalHasOneWriter(t *testing.T) {
	l, k, _, calls := linearRig(t)
	a := linearApp("verifier")
	enrollLinear(t, l, k, a)
	start := make(chan struct{})
	results := make(chan error, 2)
	for range 2 {
		go func() { <-start; _, err := l.Maintain(context.Background(), a); results <- err }()
	}
	close(start)
	for range 2 {
		if err := <-results; err != nil {
			t.Fatal(err)
		}
	}
	if len(*calls) != 1 {
		t.Fatalf("dual writer: %d requests", len(*calls))
	}
}

func TestLinearPublicationRestrictsAccessAndUsesStdin(t *testing.T) {
	var commands [][]string
	h := linearHostEntries{home: "/fake/home", access: func(string, string) (AppAccess, error) {
		a := brokerOnly()
		a.Entries[1].Apps = append(a.Entries[1].Apps, security)
		return a, nil
	}, run: func(_ context.Context, args []string, input []byte) ([]byte, error) {
		commands = append(commands, slices.Clone(args))
		if slices.Contains(args, "-i") {
			text := string(input)
			if !strings.Contains(text, "-T /usr/bin/security -T "+InstalledPath) || strings.Contains(text, "-A") || strings.Contains(text, "TOKEN-SENTINEL") {
				t.Fatal("unsafe trust or token transport")
			}
			return nil, nil
		}
		return []byte(`{"access_token":"TOKEN-SENTINEL","expires_at":"2026-11-26T12:00:00Z"}`), nil
	}}
	value := []byte(`{"access_token":"TOKEN-SENTINEL","expires_at":"2026-11-26T12:00:00Z"}`)
	if err := h.Publish(context.Background(), "token-verifier", "token-verifier", value); err != nil {
		t.Fatal(err)
	}
	for _, args := range commands {
		if strings.Contains(strings.Join(args, " "), "TOKEN-SENTINEL") {
			t.Fatal("token in argv")
		}
	}
}
