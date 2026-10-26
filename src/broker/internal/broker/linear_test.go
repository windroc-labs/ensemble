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
	items map[string][]byte
	fail  string
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
	return k.fakeApps.AppAccess(s, a)
}

type linearHostFake struct {
	mu     sync.Mutex
	value  []byte
	fail   bool
	writes int
}

func (h *linearHostFake) Read(string, string) ([]byte, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.value == nil {
		return nil, ErrNotFound
	}
	return bytes.Clone(h.value), nil
}
func (h *linearHostFake) Publish(_ context.Context, _, _ string, v []byte) error {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.writes++
	if h.fail {
		return errors.New("TOKEN-SENTINEL")
	}
	h.value = bytes.Clone(v)
	return nil
}

type linearRoundTrip func(*http.Request) (*http.Response, error)

func (f linearRoundTrip) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func linearRig(t *testing.T) (Linear, *linearKeysFake, *linearHostFake, *[]url.Values) {
	t.Helper()
	k := &linearKeysFake{items: map[string][]byte{}}
	h := &linearHostFake{}
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
	for _, kind := range []string{"harness", "manager", "role"} {
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
				if form.Get("scope") != a.scopes() || form.Get("client_secret") != "ROOT-SENTINEL" || form.Get("client_id") != a.ClientID {
					t.Fatal("wrong identity/scope")
				}
			}
			var token map[string]any
			json.Unmarshal(h.value, &token)
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
