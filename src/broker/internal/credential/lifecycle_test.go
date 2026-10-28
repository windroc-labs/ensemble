package credential

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"slices"
	"strings"
	"testing"
	"time"

	"ensemble/broker/internal/protocol"
)

var bg = context.Background()

func acquire(t *testing.T, l Lifecycle, host string, p *publisher) []protocol.Expiry {
	t.Helper()
	got, err := l.Acquire(bg, to(host, p), "app", map[string]string{"scope": "read"})
	if err != nil {
		t.Fatal(err)
	}
	return got
}

// TestStaticProviderRefused: every operation checks its provider's kind, so no static credential runs through renewal.
func TestStaticProviderRefused(t *testing.T) {
	p := newProvider(t, "acme")
	p.kind = Claude
	l := lifecycle(newStore(), p, nil)
	pub := &publisher{}
	if _, err := l.Acquire(bg, to(hostA, pub), "app", nil); !errors.Is(err, ErrStatic) {
		t.Fatalf("Acquire: %v", err)
	}
	if _, err := l.Refresh(bg, to(hostA, pub), nil); !errors.Is(err, ErrStatic) {
		t.Fatalf("Refresh: %v", err)
	}
	if len(p.calls) != 0 || pub.calls != 0 {
		t.Fatalf("static kind reached the provider %q or a consumer", p.calls)
	}
	p.kind = Linear
	acquire(t, l, hostA, pub)
}

func TestRefreshTouchesOnlyItsConsumer(t *testing.T) {
	store, p := newStore(), newProvider(t, "acme")
	l := lifecycle(store, p, nil)
	acquire(t, l, hostA, nil)
	acquire(t, l, hostB, nil)
	before := slices.Clone(store.items[records+"/"+idB])
	if _, err := l.Refresh(bg, to(hostA, nil), nil); err != nil {
		t.Fatal(err)
	}
	rec, _ := store.record(t, idA)
	wantTokens(t, "A", tokens(rec), map[string]State{"tok_3": Delivered})
	if !bytes.Equal(store.items[records+"/"+idB], before) {
		t.Fatal("B touched")
	}
}

// TestRefreshAudit: a refresh notes the renewal, then the replaced material's withdrawal, each keyed by its account
// and expiry, never its value.
func TestRefreshAudit(t *testing.T) {
	store, p := newStore(), newProvider(t, "acme")
	rec, log := recorder(idA)
	l := lifecycle(store, p, rec)
	acquire(t, l, hostA, nil)
	if _, err := l.Refresh(bg, to(hostA, nil), nil); err != nil {
		t.Fatal(err)
	}
	if err := rec.Flush(nil); err != nil {
		t.Fatal(err)
	}
	want := `"credentials":[` +
		`{"kind":"test-token","result":"issued","expires_at":"2026-10-26T13:00:00Z","account":"acme","installation":1},` +
		`{"kind":"test-token","result":"renewed","expires_at":"2026-10-26T13:00:00Z","account":"acme","installation":1},` +
		`{"kind":"test-token","result":"withdrawn","expires_at":"2026-10-26T13:00:00Z","account":"acme","installation":1}]`
	if len(log.data) != 1 || !strings.Contains(log.text(), want) || strings.Contains(log.text(), "tok_") {
		t.Fatalf("audit %s", log.text())
	}
}

func TestLegacyRecordRefreshes(t *testing.T) {
	store, p := newStore(), newProvider(t)
	legacy := `{"app":"app","tokens":[{"account":"acme","installation":1,"token":"old","expires_at":"2026-10-26T13:00:00Z"}]}`
	store.items[records+"/"+idA] = []byte(legacy)
	pub := &publisher{}
	got, err := lifecycle(store, p, nil).Refresh(bg, to(hostA, pub), map[string]string{"scope": "read"})
	if err != nil {
		t.Fatal(err)
	}
	if want := []protocol.Expiry{{Account: "acme", ExpiresAt: "2026-10-26T13:00:00Z"}}; !slices.Equal(got, want) || !slices.Equal(pub.published, []string{"tok_1"}) {
		t.Fatalf("expiries %v, published %q", got, pub.published)
	}
	rec, _ := store.record(t, idA)
	if len(rec.Tokens) != 1 || rec.Tokens[0].Token != "tok_1" || rec.Tokens[0].State != Delivered || rec.Tokens[0].Revision != 1 {
		t.Fatalf("record %+v", rec)
	}
}

func TestRecordKeepsLegacyFields(t *testing.T) {
	store, p := newStore(), newProvider(t, "acme")
	acquire(t, lifecycle(store, p, nil), hostA, nil)
	var raw struct {
		App    string           `json:"app"`
		Tokens []map[string]any `json:"tokens"`
	}
	if err := json.Unmarshal(store.items[records+"/"+idA], &raw); err != nil {
		t.Fatal(err)
	}
	m := raw.Tokens[0]
	if raw.App != "app" || m["account"] != "acme" || m["installation"] != float64(1) || m["token"] != "tok_1" || m["expires_at"] != "2026-10-26T13:00:00Z" ||
		m["revision"] != float64(1) || m["state"] != "delivered" || len(m) != 6 {
		t.Fatalf("%s", store.items[records+"/"+idA])
	}
}

func TestExpiriesListOnlyDelivered(t *testing.T) {
	store, p := newStore(), newProvider(t)
	at := clock.Add(30 * time.Minute).Format(time.RFC3339)
	store.seed(t, idA, Record{Subject: "app", Tokens: []Material{
		{Account: "acme", Target: 1, Token: "live", ExpiresAt: at, Revision: 1, State: Delivered},
		{Account: "acme", Target: 1, Token: "prepared", ExpiresAt: at, Revision: 2, State: Prepared},
		{Account: "acme", Target: 1, Token: "retiring", ExpiresAt: at, Revision: 0, State: Retiring},
	}})
	got, err := lifecycle(store, p, nil).Refresh(bg, to(hostA, nil), nil)
	if err != nil {
		t.Fatal(err)
	}
	if want := []protocol.Expiry{{Account: "acme", ExpiresAt: "2026-10-26T13:00:00Z"}}; !slices.Equal(got, want) {
		t.Fatalf("expiries %v", got)
	}
	rec, _ := store.record(t, idA)
	wantTokens(t, "record", tokens(rec), map[string]State{"tok_1": Delivered})
	if rec.Tokens[0].Revision != 3 {
		t.Fatalf("revision %d, want 3", rec.Tokens[0].Revision)
	}
}

// TestAcquireFailure: a failed acquire withdraws what it appended, but for a failed publication, which leaves its
// materials prepared; none counts as delivered.
func TestAcquireFailure(t *testing.T) {
	for point, want := range map[string]map[string]State{
		"record":  {},
		"publish": {"tok_1": Prepared, "tok_2": Prepared},
		"mark":    {},
	} {
		t.Run(point, func(t *testing.T) {
			store, p := newStore(), newProvider(t, "acme", "other")
			pub := &publisher{}
			writes := 0
			store.fail = func(op, service string) bool {
				if service != records || op != "write" {
					return false
				}
				writes++
				return point == "record" && writes == 2 || point == "mark" && writes == 3
			}
			if point == "publish" {
				pub.fail = func(n int) bool { return n == 2 }
			}
			if _, err := lifecycle(store, p, nil).Acquire(bg, to(hostA, pub), "app", nil); err == nil {
				t.Fatal("acquire succeeded")
			}
			rec, _ := store.record(t, idA)
			wantTokens(t, "record", tokens(rec), want)
		})
	}
}

func TestAcquireRecordsBeforeNextIssue(t *testing.T) {
	store, p := newStore(), newProvider(t, "acme", "other", "third")
	p.onIssue = func(n int) {
		rec, _ := store.record(t, idA)
		if len(rec.Tokens) != n-1 {
			t.Errorf("issue %d with %d recorded", n, len(rec.Tokens))
		}
		for _, m := range rec.Tokens {
			if m.State != Prepared {
				t.Errorf("%s %s before delivery", m.Token, m.State)
			}
		}
	}
	p.issueErr = func(n int) error {
		if n == 3 {
			return errors.New("killed")
		}
		return nil
	}
	if _, err := lifecycle(store, p, nil).Acquire(bg, to(hostA, nil), "app", nil); err == nil {
		t.Fatal("acquire succeeded")
	}
	if _, ok := store.record(t, idA); ok {
		t.Fatal("a failed acquire kept what it appended")
	}
}

func TestRefreshOrder(t *testing.T) {
	store, p := newStore(), newProvider(t, "acme")
	l := lifecycle(store, p, nil)
	acquire(t, l, hostA, nil)
	var log []string
	store.log, p.log = &log, &log
	pub := &publisher{log: &log}
	states := []map[string]State{}
	store.fail = func(op, service string) bool {
		if op == "write" && service == records {
			rec, _ := store.record(t, idA)
			states = append(states, tokens(rec))
		}
		return false
	}
	if _, err := l.Refresh(bg, to(hostA, pub), nil); err != nil {
		t.Fatal(err)
	}
	if want := []string{"issue tok_2", "write " + records, "publish tok_2", "write " + records, "write " + records}; !slices.Equal(log, want) {
		t.Fatalf("order %q", log)
	}
	rec, _ := store.record(t, idA)
	states = append(states, tokens(rec))
	want := []map[string]State{
		{"tok_1": Delivered},
		{"tok_1": Delivered, "tok_2": Prepared},
		{"tok_1": Retiring, "tok_2": Delivered},
		{"tok_2": Delivered},
	}
	for i := range want {
		wantTokens(t, fmt.Sprintf("record before write %d", i), states[i], want[i])
	}
}

// TestPublishFailureKeepsRecord: a failed publication keeps the new material prepared beside the usable one, in Acquire
// and in Refresh.
func TestPublishFailureKeepsRecord(t *testing.T) {
	store, p := newStore(), newProvider(t, "account")
	l := lifecycle(store, p, nil)
	published := errors.New("deadline write failed")
	d := to(hostA, nil)
	d.Publish = func(Material) error { return published }
	if _, err := l.Acquire(bg, d, "app", nil); !errors.Is(err, published) {
		t.Fatalf("%v", err)
	}
	rec, _ := store.record(t, idA)
	wantTokens(t, "after failed acquire", tokens(rec), map[string]State{"tok_1": Prepared})
	acquire(t, l, hostB, nil)
	if _, err := l.Refresh(bg, to(hostB, failing()), nil); err == nil {
		t.Fatal("refresh succeeded")
	}
	rec, _ = store.record(t, idB)
	wantTokens(t, "after failed refresh", tokens(rec), map[string]State{"tok_2": Delivered, "tok_3": Prepared})
}

func TestRefreshResumesAfterEachStep(t *testing.T) {
	at := clock.Add(30 * time.Minute).Format(time.RFC3339)
	m := func(token string, state State) Material {
		return Material{Account: "acme", Target: 1, Token: token, ExpiresAt: at, State: state}
	}
	for name, left := range map[string][]Material{
		"after the prepared write": {m("old", Delivered), m("new", Prepared)},
		"after the delivered mark": {m("old", Retiring), m("new", Delivered)},
		"after a stopped acquire":  {m("new", Prepared)},
	} {
		t.Run(name, func(t *testing.T) {
			store, p := newStore(), newProvider(t)
			store.seed(t, idA, Record{Subject: "app", Tokens: left})
			pub := &publisher{}
			if _, err := lifecycle(store, p, nil).Refresh(bg, to(hostA, pub), nil); err != nil {
				t.Fatal(err)
			}
			rec, _ := store.record(t, idA)
			wantTokens(t, "record", tokens(rec), map[string]State{"tok_1": Delivered})
			if !slices.Equal(pub.published, []string{"tok_1"}) {
				t.Fatalf("published %q", pub.published)
			}
		})
	}
}

func TestRefreshReplacesOnlyItsSlot(t *testing.T) {
	store, p := newStore(), newProvider(t, "acme", "other")
	l := lifecycle(store, p, nil)
	acquire(t, l, hostA, nil)
	p.issueErr = func(n int) error {
		if n == 4 {
			return errors.New("Linear 500")
		}
		return nil
	}
	if _, err := l.Refresh(bg, to(hostA, nil), nil); err == nil {
		t.Fatal("refresh succeeded")
	}
	rec, _ := store.record(t, idA)
	wantTokens(t, "record", tokens(rec), map[string]State{"tok_3": Delivered, "tok_2": Delivered})
}

func TestInvalidConsumerFailsClosed(t *testing.T) {
	for _, host := range []string{"", "a/b"} {
		store, p := newStore(), newProvider(t, "acme")
		store.fail = func(op, service string) bool { t.Errorf("%q: store %s %s", host, op, service); return false }
		l := lifecycle(store, p, nil)
		if _, err := l.Acquire(bg, to(host, nil), "app", nil); !errors.Is(err, ErrConsumer) {
			t.Errorf("%q acquire: %v", host, err)
		}
		if _, err := l.Refresh(bg, to(host, nil), nil); !errors.Is(err, ErrConsumer) {
			t.Errorf("%q refresh: %v", host, err)
		}
		if len(p.calls) != 0 {
			t.Errorf("%q: provider %q", host, p.calls)
		}
	}
}

func TestRefreshWithoutRecordIssuesNothing(t *testing.T) {
	store, p := newStore(), newProvider(t, "acme")
	l := lifecycle(store, p, nil)
	acquire(t, l, hostA, nil)
	before, calls := slices.Clone(store.items[records+"/"+idA]), len(p.calls)
	pub := &publisher{}
	if _, err := l.Refresh(bg, to(hostB, pub), nil); !errors.Is(err, ErrNoRecord) {
		t.Fatalf("%v", err)
	}
	if len(p.calls) != calls || pub.calls != 0 || !bytes.Equal(store.items[records+"/"+idA], before) {
		t.Fatal("a consumer without a record reached the provider, a publication or another record")
	}
}

func TestSentinelNeverLeaks(t *testing.T) {
	const issuer = "jwt_ISSUER_SECRET"
	check := func(t *testing.T, what, text string) {
		t.Helper()
		for _, s := range []string{issuer, "tok_", "STORE_SENTINEL"} {
			if strings.Contains(text, s) {
				t.Fatalf("%s leaks %s: %s", what, s, text)
			}
		}
	}
	store, p := newStore(), newProvider(t, "acme")
	rec, log := recorder(idA)
	l := lifecycle(store, p, rec)
	p.issueErr = func(int) error { return errors.New("Linear rejected " + issuer) }
	_, err := l.Acquire(bg, to(hostA, nil), "app", nil)
	check(t, "issue error", fmt.Sprint(err))
	p.issueErr = func(int) error { return protocol.New(protocol.InternalError, "Linear rejected "+issuer) }
	_, err = l.Acquire(bg, to(hostA, nil), "app", nil)
	var pe *protocol.Error
	if !errors.As(err, &pe) || pe.Code != protocol.InternalError {
		t.Fatalf("protocol error lost: %v", err)
	}
	check(t, "protocol issue error", pe.Message)
	p.issueErr = nil
	acquire(t, l, hostA, nil)
	d := to(hostA, nil)
	d.Publish = func(Material) error { return errors.New("publishing with " + issuer + " failed") }
	_, err = l.Refresh(bg, d, nil)
	check(t, "publish error", fmt.Sprint(err))
	store.fail = func(string, string) bool { return true }
	_, err = l.Refresh(bg, to(hostA, nil), nil)
	check(t, "refresh error", fmt.Sprint(err))
	_ = rec.Flush(nil)
	check(t, "audit", log.text())
	if !errors.Is(err, ErrStore) {
		t.Fatalf("store error class lost: %v", err)
	}
}

func TestIndependentLocks(t *testing.T) {
	var heldA, heldB bool
	release := make(chan struct{})
	taken := make(chan struct{})
	a := lifecycle(newStore(), newProvider(t, "acme"), nil)
	a.Lock = func() (func(), error) {
		heldA = true
		return func() { heldA = false }, nil
	}
	go func() { // a caller holding A's lock, blocked
		unlock, _ := a.Lock()
		close(taken)
		<-release
		unlock()
	}()
	<-taken
	pb := newProvider(t, "acme")
	pb.held = &heldB
	b := lifecycle(newStore(), pb, nil)
	b.Lock = heldLock(&heldB)
	done := make(chan error, 1)
	go func() {
		_, err := b.Acquire(bg, to(hostB, nil), "app", nil)
		if err == nil {
			_, err = b.Refresh(bg, to(hostB, nil), nil)
		}
		done <- err
	}()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("an unrelated lifecycle waited on a held lock")
	}
	if !heldA {
		t.Fatal("A's lock was not held throughout")
	}
	close(release)
}

func TestRefreshRecordFailure(t *testing.T) {
	store, p := newStore(), newProvider(t, "acme")
	l := lifecycle(store, p, nil)
	acquire(t, l, hostA, nil)
	store.fail = func(op, service string) bool { return service == records && op == "write" }
	pub := &publisher{}
	if _, err := l.Refresh(bg, to(hostA, pub), nil); !errors.Is(err, ErrStore) {
		t.Fatalf("%v", err)
	}
	if pub.calls != 0 {
		t.Fatal("an unrecorded replacement was published")
	}
	rec, _ := store.record(t, idA)
	wantTokens(t, "record", tokens(rec), map[string]State{"tok_1": Delivered})
}

func TestTargetsErrorRedacted(t *testing.T) {
	p := &secretProvider{newProvider(t, "acme")}
	_, err := lifecycle(newStore(), p, nil).Acquire(bg, to(hostA, nil), "app", nil)
	if err == nil || strings.Contains(err.Error(), "jwt_ISSUER_SECRET") {
		t.Fatalf("targets error: %v", err)
	}
}

// secretProvider fails Targets with its issuer secret in the error text.
type secretProvider struct{ *fakeProvider }

func (p *secretProvider) Targets(context.Context) ([]Material, error) {
	return nil, errors.New("listing with jwt_ISSUER_SECRET failed")
}

func (p *secretProvider) Begin(context.Context, string) (Issuer, error) { return p, nil }

func TestAcquireWithoutTargets(t *testing.T) {
	store := newStore()
	got, err := lifecycle(store, newProvider(t), nil).Acquire(bg, to(hostA, nil), "app", nil)
	if err != nil || got == nil || len(got) != 0 || len(store.items) != 0 {
		t.Fatalf("%v %v %v", got, err, store.items)
	}
}

func TestRefreshMarkWithoutReplacement(t *testing.T) {
	store, p := newStore(), newProvider(t, "acme")
	l := lifecycle(store, p, nil)
	acquire(t, l, hostA, nil)
	before := slices.Clone(store.items[records+"/"+idA])
	pub := &publisher{fail: func(int) bool {
		store.items[records+"/"+idA] = slices.Clone(before) // another command took the replacement out meanwhile
		return false
	}}
	if _, err := l.Refresh(bg, to(hostA, pub), nil); !errors.Is(err, ErrReleased) {
		t.Fatalf("%v", err)
	}
	rec, _ := store.record(t, idA)
	wantTokens(t, "record", tokens(rec), map[string]State{"tok_1": Delivered})
}

// TestRefreshUsesRecordedScope: a renewal never widens a grant; a record an older broker wrote, with no scope, renews
// with the caller's.
func TestRefreshUsesRecordedScope(t *testing.T) {
	recorded, caller := map[string]string{"scope": "read"}, map[string]string{"scope": "write"}
	store, p := newStore(), newProvider(t)
	store.seed(t, idA, Record{Subject: "app", Scope: recorded, Tokens: []Material{{Account: "acme", Target: 1, Token: "old", State: Delivered}}})
	store.seed(t, idB, Record{Subject: "app", Tokens: []Material{{Account: "acme", Target: 1, Token: "legacy"}}})
	l := lifecycle(store, p, nil)
	for _, host := range []string{hostA, hostB} {
		if _, err := l.Refresh(bg, to(host, nil), caller); err != nil {
			t.Fatal(err)
		}
	}
	if len(p.scopes) != 2 || !maps.Equal(p.scopes[0], recorded) || !maps.Equal(p.scopes[1], caller) {
		t.Fatalf("scopes %v", p.scopes)
	}
}

// TestPublishBeforeDelivered: Publish runs once the material is recorded, before it counts as delivered.
func TestPublishBeforeDelivered(t *testing.T) {
	store, p := newStore(), newProvider(t, "acme")
	l := lifecycle(store, p, nil)
	acquire(t, l, hostA, nil)
	var seen map[string]State
	d := to(hostA, nil)
	d.Publish = func(m Material) error {
		rec, _ := store.record(t, idA)
		seen = tokens(rec)
		if m.Token != "tok_2" {
			t.Errorf("published %+v", m)
		}
		return nil
	}
	if _, err := l.Refresh(bg, d, nil); err != nil {
		t.Fatal(err)
	}
	wantTokens(t, "record at publication", seen, map[string]State{"tok_1": Delivered, "tok_2": Prepared})
}

// TestRefreshReissuedTokenNotDuplicated: a provider that issues material the slot already holds (an account not yet
// renewed) gets that entry delivered anew, never a duplicate.
func TestRefreshReissuedTokenNotDuplicated(t *testing.T) {
	store, p := newStore(), newProvider(t, "account")
	p.fixed = "acct_1"
	l := lifecycle(store, p, nil)
	acquire(t, l, hostA, nil)
	if _, err := l.Refresh(bg, to(hostA, nil), nil); err != nil {
		t.Fatal(err)
	}
	rec, _ := store.record(t, idA)
	if len(rec.Tokens) != 1 || rec.Tokens[0].State != Delivered || rec.Tokens[0].Revision != 1 {
		t.Fatalf("record %+v", rec.Tokens)
	}
	if _, err := l.Refresh(bg, to(hostA, failing()), nil); err == nil {
		t.Fatal("refresh succeeded")
	}
	rec, _ = store.record(t, idA)
	if len(rec.Tokens) != 1 || rec.Tokens[0].State != Prepared {
		t.Fatalf("record after failed republication %+v", rec.Tokens)
	}
	acquire(t, l, hostA, nil)
	rec, _ = store.record(t, idA)
	if len(rec.Tokens) != 1 || rec.Tokens[0].State != Delivered || rec.Tokens[0].Revision != 1 {
		t.Fatalf("record after a second acquire %+v", rec.Tokens)
	}
}

// once is true the first time it is asked about a write to the records, then false.
func once() func(op, service string) bool {
	done := false
	return func(op, service string) bool {
		if op != "write" || service != records || done {
			return false
		}
		done = true
		return true
	}
}

// TestRollbackKeepsHeldMaterial: a failed run rolls back only what it appended; a material the record already held
// (the account not renewed meanwhile) stays as it was, never withdrawn.
func TestRollbackKeepsHeldMaterial(t *testing.T) {
	for name, c := range map[string]struct {
		fixed     string // the provider's one token, re-issued every time; "": a new token per issue
		lands     bool   // the failing write lands before it reports failure
		run       func(l Lifecycle) error
		wantState State
	}{
		"refresh held, write fails": {fixed: "acct_1", wantState: Delivered, run: func(l Lifecycle) error {
			_, err := l.Refresh(bg, to(hostA, nil), nil)
			return err
		}},
		"refresh held, write lands then fails": {fixed: "acct_1", lands: true, wantState: Prepared, run: func(l Lifecycle) error {
			_, err := l.Refresh(bg, to(hostA, nil), nil)
			return err
		}},
		"refresh appended, write lands then fails": {lands: true, wantState: Delivered, run: func(l Lifecycle) error {
			_, err := l.Refresh(bg, to(hostA, nil), nil)
			return err
		}},
		"acquire held, write fails": {fixed: "acct_1", wantState: Delivered, run: func(l Lifecycle) error {
			_, err := l.Acquire(bg, to(hostA, nil), "app", nil)
			return err
		}},
		"acquire appended, write lands then fails": {lands: true, wantState: Delivered, run: func(l Lifecycle) error {
			_, err := l.Acquire(bg, to(hostA, nil), "app", nil)
			return err
		}},
	} {
		t.Run(name, func(t *testing.T) {
			store, p := newStore(), newProvider(t, "account")
			p.fixed = c.fixed
			l := lifecycle(store, p, nil)
			acquire(t, l, hostA, nil)
			before, _ := store.record(t, idA)
			if c.lands {
				store.failAfter = once()
			} else {
				store.fail = once()
			}
			if err := c.run(l); !errors.Is(err, ErrStore) {
				t.Fatalf("%v", err)
			}
			rec, ok := store.record(t, idA)
			want := before.Tokens[0]
			want.State = c.wantState
			if !ok || len(rec.Tokens) != 1 || rec.Tokens[0] != want {
				t.Fatalf("record %+v (found %v), want [%+v]", rec.Tokens, ok, want)
			}
		})
	}
}
