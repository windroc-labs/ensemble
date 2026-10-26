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
	records  = "test-tokens"
	backlogS = "test-revocations"
	ctrA     = "ensemble-a-00000000000000000000000000000001"
	ctrB     = "ensemble-b-00000000000000000000000000000002"
)

var errMissing = errors.New("missing")

// memStore is a persistent fake Keychain: a new Lifecycle over it sees what an earlier one wrote. fail injects a
// failure by operation (read, write, delete) and service; its error carries a sentinel no output may show.
type memStore struct {
	items map[string][]byte
	fail  func(op, service string) bool
	log   *[]string
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

func (s *memStore) backlog(t *testing.T) []Pending {
	t.Helper()
	var list []Pending
	if v, ok := s.items[backlogS+"/pending"]; ok {
		if err := json.Unmarshal(v, &list); err != nil {
			t.Fatal(err)
		}
	}
	return list
}

func (s *memStore) seed(t *testing.T, id string, rec Record) {
	t.Helper()
	data, err := json.Marshal(rec)
	if err != nil {
		t.Fatal(err)
	}
	s.items[records+"/"+id] = data
}

// fakeProvider issues tok_<n> per Issue call, one target per account, and revokes by p.revoke (nil: revoked). It
// fails the test if a lifecycle lock is held across one of its requests.
type fakeProvider struct {
	t        *testing.T
	kind     Kind
	now      time.Time
	targets  []Material
	n        int
	issueErr func(n int) error
	onIssue  func(n int)
	revoke   func(token string) Revocation
	revoked  []string
	calls    []string
	held     *bool
	log      *[]string
}

func newProvider(t *testing.T, accounts ...string) *fakeProvider {
	p := &fakeProvider{t: t, kind: GitHub, now: clock}
	for i, a := range accounts {
		p.targets = append(p.targets, Material{Account: a, Target: int64(i + 1)})
	}
	return p
}

func (p *fakeProvider) unlocked(what string) {
	if p.held != nil && *p.held {
		p.t.Errorf("%s while a lifecycle lock is held", what)
	}
}

func (p *fakeProvider) Kind() Kind        { return p.kind }
func (p *fakeProvider) AuditKind() string { return "test-token" }
func (p *fakeProvider) Now() time.Time    { return p.now }
func (p *fakeProvider) Secret() string    { return "jwt_ISSUER_SECRET" }

func (p *fakeProvider) Begin(context.Context, string) (Issuer, error) {
	p.calls = append(p.calls, "begin")
	return p, nil
}

func (p *fakeProvider) Targets(context.Context) ([]Material, error) { return p.targets, nil }

func (p *fakeProvider) Issue(_ context.Context, _ map[string]string, target Material) (Material, error) {
	p.unlocked("issue")
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
	p.calls = append(p.calls, "issue "+token)
	if p.log != nil {
		*p.log = append(*p.log, "issue "+token)
	}
	return Material{Account: target.Account, Target: target.Target, Token: token, ExpiresAt: p.now.Add(time.Hour).Format(time.RFC3339)}, nil
}

func (p *fakeProvider) File(m Material) (File, error) {
	if m.Account == "bad/account" {
		return File{}, errors.New("account refused")
	}
	return File{Dir: "/run/test", Name: m.Account, Replace: true}, nil
}

func (p *fakeProvider) Revoke(_ context.Context, m Material) Revocation {
	p.unlocked("revoke")
	p.calls = append(p.calls, "revoke "+m.Token)
	if p.log != nil {
		*p.log = append(*p.log, "revoke "+m.Token)
	}
	r := Revocation{Outcome: Revoked}
	if p.revoke != nil {
		r = p.revoke(m.Token)
	}
	if r.Outcome == Revoked || r.Outcome == AlreadyInvalid {
		p.revoked = append(p.revoked, m.Token)
	}
	return r
}

func (p *fakeProvider) issues() int {
	return len(slices.DeleteFunc(slices.Clone(p.calls), func(c string) bool { return !strings.HasPrefix(c, "issue ") }))
}

func (p *fakeProvider) revokes() int {
	return len(slices.DeleteFunc(slices.Clone(p.calls), func(c string) bool { return !strings.HasPrefix(c, "revoke ") }))
}

type sharedProvider struct{ *fakeProvider }

func (sharedProvider) SharedAccount() bool { return true }

func failRevoke(string) Revocation {
	return Revocation{Outcome: Retryable, Err: errors.New("GitHub 503")}
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

func recorder(container string) (*audit.Recorder, *lines) {
	l := &lines{}
	r := &audit.Recorder{Command: "close", Log: l, Now: func() time.Time { return clock }}
	r.Begin(container, nil)
	return r, l
}

func lifecycle(store *memStore, p Provider, rec *audit.Recorder) Lifecycle {
	return Lifecycle{
		Provider: p,
		Store:    store,
		NotFound: errMissing,
		Records:  records,
		Backlog:  [2]string{backlogS, "pending"},
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

// container is an exec into one container: it records each delivery and fails while fail says so.
type container struct {
	files map[string]string
	argv  [][]string
	fail  func(n int) bool
	log   *[]string
}

func newContainer() *container { return &container{files: map[string]string{}} }

func (c *container) exec(argv []string, stdin []byte) error {
	c.argv = append(c.argv, argv)
	if c.fail != nil && c.fail(len(c.argv)) {
		return errors.New("exec failed: " + string(stdin))
	}
	if c.log != nil {
		*c.log = append(*c.log, "deliver "+string(stdin))
	}
	c.files[argv[len(argv)-1]] = string(stdin)
	return nil
}

func to(id string, c *container) Delivery { return Delivery{Consumer{Container: id}, c.exec} }

func tokens(rec Record) map[string]State {
	out := map[string]State{}
	for _, m := range rec.Tokens {
		out[m.Token] = m.Current()
	}
	return out
}

func pendingTokens(list []Pending) []string {
	out := []string{}
	for _, p := range list {
		out = append(out, p.Token)
	}
	return out
}

func wantTokens(t *testing.T, what string, got map[string]State, want map[string]State) {
	t.Helper()
	if !maps.Equal(got, want) {
		t.Fatalf("%s: %v, want %v", what, got, want)
	}
}
