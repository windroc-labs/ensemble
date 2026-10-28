package credential

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"slices"
	"strings"
	"testing"
	"time"

	"ensemble/broker/internal/audit"
)

var clock = time.Date(2026, 10, 26, 12, 0, 0, 0, time.UTC)

const (
	records = "test-grants"
	hostA   = "linear-a"
	hostB   = "linear-b"
	idA     = "host/" + hostA
	idB     = "host/" + hostB
)

var errMissing = errors.New("missing")

// memStore is a persistent fake Keychain: a new Lifecycle over it sees what an earlier one wrote. fail injects a
// failure by operation (read, write, delete) and service; its error carries a sentinel no output may show.
type memStore struct {
	items     map[string][]byte
	fail      func(op, service string) bool
	failAfter func(op, service string) bool // the write lands, then reports failure, as a partly failed Keychain call
	log       *[]string
}

func newStore() *memStore { return &memStore{items: map[string][]byte{}} }

func (s *memStore) broken(op, service string) error {
	if s.fail != nil && s.fail(op, service) {
		return errors.New("keychain broke: STORE_SENTINEL")
	}
	return nil
}

func (s *memStore) Secret(service, account string) ([]byte, error) {
	if err := s.broken("read", service); err != nil {
		return nil, err
	}
	v, ok := s.items[service+"/"+account]
	if !ok {
		return nil, errMissing
	}
	return slices.Clone(v), nil
}

func (s *memStore) PutSecret(service, account string, data []byte) error {
	if err := s.broken("write", service); err != nil {
		return err
	}
	if s.log != nil {
		*s.log = append(*s.log, "write "+service)
	}
	s.items[service+"/"+account] = slices.Clone(data)
	if s.failAfter != nil && s.failAfter("write", service) {
		return errors.New("keychain broke after writing: STORE_SENTINEL")
	}
	return nil
}

func (s *memStore) DeleteSecret(service, account string) error {
	if err := s.broken("delete", service); err != nil {
		return err
	}
	if s.log != nil {
		*s.log = append(*s.log, "delete "+service)
	}
	delete(s.items, service+"/"+account)
	return nil
}

func (s *memStore) record(t *testing.T, id string) (Record, bool) {
	t.Helper()
	v, ok := s.items[records+"/"+id]
	var rec Record
	if ok {
		if err := json.Unmarshal(v, &rec); err != nil {
			t.Fatal(err)
		}
	}
	return rec, ok
}

func (s *memStore) seed(t *testing.T, id string, rec Record) {
	t.Helper()
	data, err := json.Marshal(rec)
	if err != nil {
		t.Fatal(err)
	}
	s.items[records+"/"+id] = data
}

// fakeProvider issues tok_<n> per Issue call (or fixed, as an account not renewed meanwhile), one target per account.
// It fails the test if a lifecycle lock is held across one of its requests.
type fakeProvider struct {
	t        *testing.T
	kind     Kind
	now      time.Time
	targets  []Material
	n        int
	issueErr func(n int) error
	onIssue  func(n int)
	calls    []string
	held     *bool
	scopes   []map[string]string // each Issue's
	fixed    string
	log      *[]string
}

func newProvider(t *testing.T, accounts ...string) *fakeProvider {
	p := &fakeProvider{t: t, kind: Linear, now: clock}
	for i, a := range accounts {
		p.targets = append(p.targets, Material{Account: a, Target: int64(i + 1)})
	}
	return p
}

func (p *fakeProvider) Kind() Kind        { return p.kind }
func (p *fakeProvider) AuditKind() string { return "test-token" }
func (p *fakeProvider) Secret() string    { return "jwt_ISSUER_SECRET" }

func (p *fakeProvider) Begin(context.Context, string) (Issuer, error) {
	p.calls = append(p.calls, "begin")
	return p, nil
}

func (p *fakeProvider) Targets(context.Context) ([]Material, error) { return p.targets, nil }

func (p *fakeProvider) Issue(_ context.Context, scope map[string]string) (Material, error) {
	if p.held != nil && *p.held {
		p.t.Error("issue while a lifecycle lock is held")
	}
	p.scopes = append(p.scopes, scope)
	p.n++
	if p.onIssue != nil {
		p.onIssue(p.n)
	}
	if p.issueErr != nil {
		if err := p.issueErr(p.n); err != nil {
			return Material{}, err
		}
	}
	token := fmt.Sprintf("tok_%d", p.n)
	if p.fixed != "" {
		token = p.fixed
	}
	p.calls = append(p.calls, "issue "+token)
	if p.log != nil {
		*p.log = append(*p.log, "issue "+token)
	}
	return Material{Token: token, ExpiresAt: p.now.Add(time.Hour).Format(time.RFC3339)}, nil
}

// lines is the audit log a recorder flushes to.
type lines struct{ data []string }

func (l *lines) Append(b ...[]byte) error {
	for _, x := range b {
		l.data = append(l.data, string(x))
	}
	return nil
}

func (l *lines) text() string { return strings.Join(l.data, "\n") }

func recorder(id string) (*audit.Recorder, *lines) {
	l := &lines{}
	r := &audit.Recorder{Command: "linear-maintain", Log: l, Now: func() time.Time { return clock }}
	r.Begin(id, nil)
	r.Renewing("")
	return r, l
}

func lifecycle(store *memStore, p Provider, rec *audit.Recorder) Lifecycle {
	return Lifecycle{
		Provider: p,
		Store:    store,
		NotFound: errMissing,
		Records:  records,
		Lock:     func() (func(), error) { return func() {}, nil },
		Audit:    rec,
	}
}

// heldLock is a lock that records, for the provider, whether it is held.
func heldLock(held *bool) func() (func(), error) {
	return func() (func(), error) {
		*held = true
		return func() { *held = false }, nil
	}
}

// publisher records each publication and fails while fail says so.
type publisher struct {
	published []string
	fail      func(n int) bool
	calls     int
	log       *[]string
}

func (p *publisher) publish(m Material) error {
	p.calls++
	if p.fail != nil && p.fail(p.calls) {
		return errors.New("publish failed: " + m.Token)
	}
	if p.log != nil {
		*p.log = append(*p.log, "publish "+m.Token)
	}
	p.published = append(p.published, m.Token)
	return nil
}

func to(host string, p *publisher) Delivery {
	d := Delivery{Consumer: Consumer{Host: host}}
	if p != nil {
		d.Publish = p.publish
	}
	return d
}

func failing() *publisher { return &publisher{fail: func(int) bool { return true }} }

func tokens(rec Record) map[string]State {
	out := map[string]State{}
	for _, m := range rec.Tokens {
		out[m.Token] = m.State
	}
	return out
}

func wantTokens(t *testing.T, what string, got map[string]State, want map[string]State) {
	t.Helper()
	if !maps.Equal(got, want) {
		t.Fatalf("%s: %v, want %v", what, got, want)
	}
}
