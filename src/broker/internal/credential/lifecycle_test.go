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

func acquire(t *testing.T, l Lifecycle, id string, c *container) []protocol.Expiry {
	t.Helper()
	got, err := l.Acquire(bg, to(id, c), "app", map[string]string{"contents": "read"})
	if err != nil {
		t.Fatal(err)
	}
	return got
}

// TestStaticProviderRefused: every operation checks its provider's kind, so no static credential runs through renewal
// or revocation.
func TestStaticProviderRefused(t *testing.T) {
	p := newProvider(t, "acme")
	p.kind = Claude
	l := lifecycle(newStore(), p, nil)
	c := newContainer()
	if _, err := l.Acquire(bg, to(ctrA, c), "app", nil); !errors.Is(err, ErrStatic) {
		t.Fatalf("Acquire: %v", err)
	}
	if _, err := l.Refresh(bg, to(ctrA, c), nil); !errors.Is(err, ErrStatic) {
		t.Fatalf("Refresh: %v", err)
	}
	if err := l.Release(bg, Consumer{Container: ctrA}); !errors.Is(err, ErrStatic) {
		t.Fatalf("Release: %v", err)
	}
	if _, err := l.Retry(bg); !errors.Is(err, ErrStatic) {
		t.Fatalf("Retry: %v", err)
	}
	if len(p.calls) != 0 || len(c.argv) != 0 {
		t.Fatalf("static kind reached the provider %q or a container %q", p.calls, c.argv)
	}
	p.kind = GitHub
	acquire(t, l, ctrA, c)
}

func TestRevocationOutcomes(t *testing.T) {
	for _, tc := range []struct {
		outcome Outcome
		gone    bool
		audit   string
	}{
		{Revoked, true, `"result":"revoked"`},
		{AlreadyInvalid, true, `"result":"revoked"`},
		{Retryable, false, `"result":"pending","reason":"GitHub 503"`},
		{Unsupported, false, `"result":"pending","reason":"revocation unsupported"`},
	} {
		t.Run(string(tc.outcome), func(t *testing.T) {
			store, p := newStore(), newProvider(t, "acme")
			rec, log := recorder(ctrA)
			l := lifecycle(store, p, rec)
			acquire(t, l, ctrA, newContainer())
			p.revoke = func(string) Revocation {
				r := Revocation{Outcome: tc.outcome}
				if tc.outcome == Retryable {
					r.Err = errors.New("GitHub 503")
				}
				return r
			}
			if err := l.Release(bg, Consumer{Container: ctrA}); err != nil {
				t.Fatal(err)
			}
			_ = rec.Flush(nil)
			if _, ok := store.record(t, ctrA); ok {
				t.Fatal("record kept after a transfer to the backlog")
			}
			if queued := pendingTokens(store.backlog(t)); slices.Equal(queued, []string{"tok_1"}) == tc.gone {
				t.Fatalf("backlog %q", queued)
			}
			if !strings.Contains(log.text(), tc.audit) {
				t.Fatalf("audit %s", log.text())
			}
		})
	}
}

func TestReleaseTouchesOnlyItsConsumer(t *testing.T) {
	store, p := newStore(), newProvider(t, "acme")
	l := lifecycle(store, p, nil)
	acquire(t, l, ctrA, newContainer())
	acquire(t, l, ctrB, newContainer())
	before := slices.Clone(store.items[records+"/"+ctrB])
	p.revoke = failRevoke
	store.fail = func(op, service string) bool { return service == backlogS }
	var unrevoked *UnrevokedError
	if err := l.Release(bg, Consumer{Container: ctrA}); !errors.As(err, &unrevoked) || unrevoked.Consumer != ctrA {
		t.Fatalf("release with revoke and backlog down: %v", err)
	}
	if rec, ok := store.record(t, ctrA); !ok || len(rec.Tokens) != 1 {
		t.Fatalf("recovery material of A lost: %v", rec)
	}
	if !bytes.Equal(store.items[records+"/"+ctrB], before) {
		t.Fatal("B's record changed")
	}
	store.fail, p.revoke = nil, nil
	if err := l.Release(bg, Consumer{Container: ctrA}); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(store.items[records+"/"+ctrB], before) || slices.Contains(p.revoked, "tok_2") {
		t.Fatal("B's grant released with A's")
	}
}

func TestRefreshTouchesOnlyItsConsumer(t *testing.T) {
	store, p := newStore(), newProvider(t, "acme")
	l := lifecycle(store, p, nil)
	acquire(t, l, ctrA, newContainer())
	acquire(t, l, ctrB, newContainer())
	before := slices.Clone(store.items[records+"/"+ctrB])
	if _, err := l.Refresh(bg, to(ctrA, newContainer()), nil); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(store.items[records+"/"+ctrB], before) || !slices.Equal(p.revoked, []string{"tok_1"}) {
		t.Fatalf("B touched; revoked %q", p.revoked)
	}
}

func TestLegacyRecordRefreshesAndReleases(t *testing.T) {
	store, p := newStore(), newProvider(t)
	legacy := `{"app":"app","tokens":[{"account":"acme","installation":1,"token":"ghs_old","expires_at":"2026-10-26T13:00:00Z"}]}`
	store.items[records+"/"+ctrA] = []byte(legacy)
	l := lifecycle(store, p, nil)
	c := newContainer()
	got, err := l.Refresh(bg, to(ctrA, c), map[string]string{"contents": "read"})
	if err != nil {
		t.Fatal(err)
	}
	if want := []protocol.Expiry{{Account: "acme", ExpiresAt: "2026-10-26T13:00:00Z"}}; !slices.Equal(got, want) || c.files["acme"] != "tok_1" {
		t.Fatalf("expiries %v, files %v", got, c.files)
	}
	rec, _ := store.record(t, ctrA)
	if len(rec.Tokens) != 1 || rec.Tokens[0].Token != "tok_1" || rec.Tokens[0].State != Delivered || rec.Tokens[0].Revision != 1 ||
		!slices.Equal(p.revoked, []string{"ghs_old"}) {
		t.Fatalf("record %+v revoked %q", rec, p.revoked)
	}
	store.items[records+"/"+ctrB] = []byte(legacy)
	if err := l.Release(bg, Consumer{Container: ctrB}); err != nil || !slices.Contains(p.revoked, "ghs_old") {
		t.Fatalf("release: %v", err)
	}
	if _, ok := store.record(t, ctrB); ok {
		t.Fatal("legacy record kept")
	}
}

func TestRecordKeepsLegacyFields(t *testing.T) {
	store, p := newStore(), newProvider(t, "acme")
	acquire(t, lifecycle(store, p, nil), ctrA, newContainer())
	var raw struct {
		App    string           `json:"app"`
		Tokens []map[string]any `json:"tokens"`
	}
	if err := json.Unmarshal(store.items[records+"/"+ctrA], &raw); err != nil {
		t.Fatal(err)
	}
	m := raw.Tokens[0]
	if raw.App != "app" || m["account"] != "acme" || m["installation"] != float64(1) || m["token"] != "tok_1" || m["expires_at"] != "2026-10-26T13:00:00Z" {
		t.Fatalf("%s", store.items[records+"/"+ctrA])
	}
}

func TestExpiriesListOnlyDelivered(t *testing.T) {
	store, p := newStore(), newProvider(t)
	at := clock.Add(30 * time.Minute).Format(time.RFC3339)
	store.seed(t, ctrA, Record{Subject: "app", Tokens: []Material{
		{Account: "acme", Target: 1, Token: "ghs_live", ExpiresAt: at, Revision: 1, State: Delivered},
		{Account: "acme", Target: 1, Token: "ghs_prepared", ExpiresAt: at, Revision: 2, State: Prepared},
		{Account: "acme", Target: 1, Token: "ghs_retiring", ExpiresAt: at, Revision: 0, State: Retiring},
	}})
	got, err := lifecycle(store, p, nil).Refresh(bg, to(ctrA, newContainer()), nil)
	if err != nil {
		t.Fatal(err)
	}
	if want := []protocol.Expiry{{Account: "acme", ExpiresAt: "2026-10-26T13:00:00Z"}}; !slices.Equal(got, want) {
		t.Fatalf("expiries %v", got)
	}
	rec, _ := store.record(t, ctrA)
	wantTokens(t, "record", tokens(rec), map[string]State{"tok_1": Delivered})
	if rec.Tokens[0].Revision != 3 {
		t.Fatalf("revision %d, want 3", rec.Tokens[0].Revision)
	}
}

func TestAcquireFailureMatrix(t *testing.T) {
	for _, point := range []string{"record", "delivery", "mark"} {
		for _, revokeOK := range []bool{true, false} {
			for _, backlogOK := range []bool{true, false} {
				t.Run(fmt.Sprintf("%s/revoke=%v/backlog=%v", point, revokeOK, backlogOK), func(t *testing.T) {
					store, p := newStore(), newProvider(t, "acme", "other")
					c := newContainer()
					writes := 0
					store.fail = func(op, service string) bool {
						if service == backlogS {
							return !backlogOK
						}
						if service != records || op != "write" {
							return false
						}
						writes++
						return point == "record" && writes == 2 || point == "mark" && writes == 3
					}
					if point == "delivery" {
						c.fail = func(n int) bool { return n == 2 }
					}
					if !revokeOK {
						p.revoke = failRevoke
					}
					_, err := lifecycle(store, p, nil).Acquire(bg, to(ctrA, c), "app", nil)
					if err == nil {
						t.Fatal("acquire succeeded")
					}
					// No issued token is lost: each is revoked, queued, or kept in the record, but for a never-recorded
					// one with every route down, which is reported unrevoked and bounded by its expiry.
					rec, _ := store.record(t, ctrA)
					var unrevoked *UnrevokedError
					for _, token := range []string{"tok_1", "tok_2"} {
						_, recorded := tokens(rec)[token]
						if !slices.Contains(p.revoked, token) && !slices.Contains(pendingTokens(store.backlog(t)), token) && !recorded &&
							!(token == "tok_2" && point == "record" && !revokeOK && !backlogOK && errors.As(err, &unrevoked)) {
							t.Fatalf("%s lost: err %v record %v", token, err, rec)
						}
					}
					for _, m := range rec.Tokens {
						if m.State == Delivered {
							t.Fatalf("a failed acquire left %s delivered", m.Token)
						}
					}
					store.fail, p.revoke = nil, nil
					rebuilt := lifecycle(store, p, nil)
					if err := rebuilt.Release(bg, Consumer{Container: ctrA}); err != nil {
						t.Fatal(err)
					}
					if _, err := rebuilt.Retry(bg); err != nil {
						t.Fatal(err)
					}
					if _, ok := store.record(t, ctrA); ok || len(store.backlog(t)) != 0 || !slices.Contains(p.revoked, "tok_1") {
						t.Fatalf("not finished: record %v backlog %v revoked %q", rec, store.backlog(t), p.revoked)
					}
				})
			}
		}
	}
}

func TestAcquireRecordsBeforeNextIssue(t *testing.T) {
	store, p := newStore(), newProvider(t, "acme", "other", "third")
	p.onIssue = func(n int) {
		rec, _ := store.record(t, ctrA)
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
	if _, err := lifecycle(store, p, nil).Acquire(bg, to(ctrA, newContainer()), "app", nil); err == nil {
		t.Fatal("acquire succeeded")
	}
	if !slices.Equal(p.revoked, []string{"tok_1", "tok_2"}) {
		t.Fatalf("revoked %q", p.revoked)
	}
}

func TestRefreshOrder(t *testing.T) {
	store, p := newStore(), newProvider(t, "acme")
	l := lifecycle(store, p, nil)
	acquire(t, l, ctrA, newContainer())
	var log []string
	store.log, p.log = &log, &log
	c := newContainer()
	c.log = &log
	states := []map[string]State{}
	store.fail = func(op, service string) bool {
		if op == "write" && service == records {
			rec, _ := store.record(t, ctrA)
			states = append(states, tokens(rec))
		}
		return false
	}
	if _, err := l.Refresh(bg, to(ctrA, c), nil); err != nil {
		t.Fatal(err)
	}
	if want := []string{"issue tok_2", "write " + records, "deliver tok_2", "write " + records, "revoke tok_1", "write " + records}; !slices.Equal(log, want) {
		t.Fatalf("order %q", log)
	}
	rec, _ := store.record(t, ctrA)
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

func TestRefreshDeliveryFailureKeepsOld(t *testing.T) {
	store, p := newStore(), newProvider(t, "acme")
	l := lifecycle(store, p, nil)
	acquire(t, l, ctrA, newContainer())
	c := newContainer()
	c.fail = func(int) bool { return true }
	if _, err := l.Refresh(bg, to(ctrA, c), nil); !errors.Is(err, ErrDelivery) {
		t.Fatalf("%v", err)
	}
	rec, _ := store.record(t, ctrA)
	wantTokens(t, "record", tokens(rec), map[string]State{"tok_1": Delivered})
	if !slices.Equal(p.revoked, []string{"tok_2"}) {
		t.Fatalf("revoked %q", p.revoked)
	}
}

func TestRefreshResumesAfterEachStep(t *testing.T) {
	at := clock.Add(30 * time.Minute).Format(time.RFC3339)
	m := func(token string, state State) Material {
		return Material{Account: "acme", Target: 1, Token: token, ExpiresAt: at, State: state}
	}
	for name, left := range map[string][]Material{
		"after the prepared write": {m("ghs_old", Delivered), m("ghs_new", Prepared)},
		"after the delivered mark": {m("ghs_old", Retiring), m("ghs_new", Delivered)},
		"after a stopped acquire":  {m("ghs_new", Prepared)},
	} {
		t.Run(name, func(t *testing.T) {
			store, p := newStore(), newProvider(t)
			store.seed(t, ctrA, Record{Subject: "app", Tokens: left})
			c := newContainer()
			if _, err := lifecycle(store, p, nil).Refresh(bg, to(ctrA, c), nil); err != nil {
				t.Fatal(err)
			}
			rec, _ := store.record(t, ctrA)
			wantTokens(t, "record", tokens(rec), map[string]State{"tok_1": Delivered})
			for _, x := range left {
				if !slices.Contains(p.revoked, x.Token) {
					t.Fatalf("%s left unretired: %q", x.Token, p.revoked)
				}
			}
			if c.files["acme"] != "tok_1" {
				t.Fatalf("files %v", c.files)
			}
		})
	}
	store, p := newStore(), newProvider(t)
	store.seed(t, ctrA, Record{Subject: "app", Tokens: []Material{m("ghs_old", Retiring), m("ghs_new", Prepared)}})
	if err := lifecycle(store, p, nil).Release(bg, Consumer{Container: ctrA}); err != nil {
		t.Fatal(err)
	}
	if _, ok := store.record(t, ctrA); ok || !slices.Equal(p.revoked, []string{"ghs_old", "ghs_new"}) {
		t.Fatalf("release left %q", p.revoked)
	}
}

func TestRefreshRetiresOnlyItsSlot(t *testing.T) {
	store, p := newStore(), newProvider(t, "acme", "other")
	l := lifecycle(store, p, nil)
	acquire(t, l, ctrA, newContainer())
	p.issueErr = func(n int) error {
		if n == 4 {
			return errors.New("GitHub 500")
		}
		return nil
	}
	if _, err := l.Refresh(bg, to(ctrA, newContainer()), nil); err == nil {
		t.Fatal("refresh succeeded")
	}
	rec, _ := store.record(t, ctrA)
	wantTokens(t, "record", tokens(rec), map[string]State{"tok_3": Delivered, "tok_2": Delivered})
	if !slices.Equal(p.revoked, []string{"tok_1"}) {
		t.Fatalf("revoked %q", p.revoked)
	}
}

func TestInvalidConsumerFailsClosed(t *testing.T) {
	exec := newContainer().exec
	for name, d := range map[string]Delivery{
		"neither":                {Consumer: Consumer{}, Exec: exec},
		"both":                   {Consumer: Consumer{Container: ctrA, Host: "linear"}, Exec: exec},
		"container without exec": {Consumer: Consumer{Container: ctrA}, Exec: nil},
		"host with exec":         {Consumer: Consumer{Host: "linear"}, Exec: exec},
		"container naming host":  {Consumer: Consumer{Container: "host/linear"}, Exec: exec},
	} {
		store, p := newStore(), newProvider(t, "acme")
		store.fail = func(op, service string) bool { t.Errorf("%s: store %s %s", name, op, service); return false }
		l := lifecycle(store, p, nil)
		if _, err := l.Acquire(bg, d, "app", nil); !errors.Is(err, ErrConsumer) {
			t.Errorf("%s acquire: %v", name, err)
		}
		if _, err := l.Refresh(bg, d, nil); !errors.Is(err, ErrConsumer) {
			t.Errorf("%s refresh: %v", name, err)
		}
		if len(p.calls) != 0 {
			t.Errorf("%s: provider %q", name, p.calls)
		}
	}
	if err := lifecycle(newStore(), newProvider(t), nil).Release(bg, Consumer{}); !errors.Is(err, ErrConsumer) {
		t.Fatalf("release: %v", err)
	}
}

func TestStaleGenerationIssuesNothing(t *testing.T) {
	store, p := newStore(), newProvider(t, "acme")
	l := lifecycle(store, p, nil)
	current := "ensemble-s-00000000000000000000000000000002"
	acquire(t, l, current, newContainer())
	before, calls := slices.Clone(store.items[records+"/"+current]), len(p.calls)
	stale := newContainer()
	if _, err := l.Refresh(bg, to("ensemble-s-00000000000000000000000000000001", stale), nil); !errors.Is(err, ErrNoRecord) {
		t.Fatalf("%v", err)
	}
	if len(p.calls) != calls || len(stale.argv) != 0 || !bytes.Equal(store.items[records+"/"+current], before) {
		t.Fatal("stale generation reached the provider, a container or the current record")
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
	rec, log := recorder(ctrA)
	l := lifecycle(store, p, rec)
	p.issueErr = func(int) error { return errors.New("GitHub rejected " + issuer) }
	_, err := l.Acquire(bg, to(ctrA, newContainer()), "app", nil)
	check(t, "issue error", fmt.Sprint(err))
	p.issueErr = func(int) error { return protocol.New(protocol.GitHubFailed, "GitHub rejected "+issuer) }
	_, err = l.Acquire(bg, to(ctrA, newContainer()), "app", nil)
	var pe *protocol.Error
	if !errors.As(err, &pe) || pe.Code != protocol.GitHubFailed {
		t.Fatalf("protocol error lost: %v", err)
	}
	check(t, "protocol issue error", pe.Message)
	p.issueErr = nil
	acquire(t, l, ctrA, newContainer())
	p.revoke = func(token string) Revocation {
		return Revocation{Outcome: Retryable, Err: errors.New("revoke of " + token + " failed")}
	}
	if err := l.Release(bg, Consumer{Container: ctrA}); err != nil {
		t.Fatal(err)
	}
	_, _ = l.Retry(bg)
	store.fail = func(string, string) bool { return true }
	_, err = l.Pending()
	check(t, "store error", fmt.Sprint(err))
	check(t, "release error", fmt.Sprint(l.Release(bg, Consumer{Container: ctrA})))
	_, err = l.Refresh(bg, to(ctrA, newContainer()), nil)
	check(t, "refresh error", fmt.Sprint(err))
	_ = rec.Flush(nil)
	check(t, "audit", log.text())
	if !strings.Contains(log.text(), "[redacted]") {
		t.Fatalf("revoke reason not kept, redacted: %s", log.text())
	}
	if !errors.Is(err, ErrStore) {
		t.Fatalf("store error class lost: %v", err)
	}
}

func TestIndependentLocks(t *testing.T) {
	var heldA, heldB bool
	release := make(chan struct{})
	taken := make(chan struct{})
	storeA := newStore()
	a := lifecycle(storeA, newProvider(t, "acme"), nil)
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
		_, err := b.Acquire(bg, to(ctrB, newContainer()), "app", nil)
		if err == nil {
			_, err = b.Refresh(bg, to(ctrB, newContainer()), nil)
		}
		if err == nil {
			err = b.Release(bg, Consumer{Container: ctrB})
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

func TestReleaseAndRetryConverge(t *testing.T) {
	store, p := newStore(), newProvider(t, "acme")
	l := lifecycle(store, p, nil)
	acquire(t, l, ctrA, newContainer())
	p.revoke = failRevoke
	if err := l.Release(bg, Consumer{Container: ctrA}); err != nil {
		t.Fatal(err)
	}
	p.revoke = nil
	if left, err := l.Retry(bg); err != nil || len(left) != 0 {
		t.Fatalf("%v %v", left, err)
	}
	calls := len(p.calls)
	if err := l.Release(bg, Consumer{Container: ctrA}); err != nil {
		t.Fatal(err)
	}
	if left, err := l.Retry(bg); err != nil || len(left) != 0 || len(p.calls) != calls {
		t.Fatalf("second release and retry not a no-op: %v %v %q", left, err, p.calls[calls:])
	}
}

func TestSharedProviderReleaseNeverRevokes(t *testing.T) {
	store, fp := newStore(), newProvider(t, "account")
	p := sharedProvider{fp}
	rec, log := recorder(ctrA)
	l := lifecycle(store, p, rec)
	acquire(t, l, ctrA, newContainer())
	acquire(t, l, ctrB, newContainer())
	before := slices.Clone(store.items[records+"/"+ctrB])
	if err := l.Release(bg, Consumer{Container: ctrA}); err != nil {
		t.Fatal(err)
	}
	_ = rec.Flush(nil)
	if _, ok := store.record(t, ctrA); ok || !bytes.Equal(store.items[records+"/"+ctrB], before) || len(store.backlog(t)) != 0 {
		t.Fatal("release of a shared grant touched more than its record")
	}
	if !strings.Contains(log.text(), `"result":"withdrawn"`) {
		t.Fatalf("audit %s", log.text())
	}
	if _, err := l.Refresh(bg, to(ctrB, newContainer()), nil); err != nil {
		t.Fatal(err)
	}
	recB, _ := store.record(t, ctrB)
	wantTokens(t, "B after refresh", tokens(recB), map[string]State{"tok_3": Delivered})
	failing := newContainer()
	failing.fail = func(int) bool { return true }
	if _, err := l.Refresh(bg, to(ctrB, failing), nil); err == nil {
		t.Fatal("refresh succeeded")
	}
	recB, _ = store.record(t, ctrB)
	wantTokens(t, "B after a failed refresh", tokens(recB), map[string]State{"tok_3": Delivered, "tok_4": Prepared})
	if _, err := l.Acquire(bg, to(ctrA, failing), "app", nil); err == nil {
		t.Fatal("acquire succeeded")
	}
	if err := l.Release(bg, Consumer{Container: ctrA}); err != nil {
		t.Fatal(err)
	}
	if _, ok := store.record(t, ctrA); ok {
		t.Fatal("release kept a failed shared acquire's record")
	}
	if fp.revokes() != 0 || len(store.backlog(t)) != 0 {
		t.Fatalf("shared material revoked: %q", fp.calls)
	}
}

func TestRefreshRecordFailure(t *testing.T) {
	for _, revokeOK := range []bool{true, false} {
		for _, backlogOK := range []bool{true, false} {
			t.Run(fmt.Sprintf("revoke=%v/backlog=%v", revokeOK, backlogOK), func(t *testing.T) {
				store, p := newStore(), newProvider(t, "acme")
				l := lifecycle(store, p, nil)
				acquire(t, l, ctrA, newContainer())
				store.fail = func(op, service string) bool {
					return service == backlogS && !backlogOK || service == records && op == "write"
				}
				if !revokeOK {
					p.revoke = failRevoke
				}
				c := newContainer()
				_, err := l.Refresh(bg, to(ctrA, c), nil)
				var unrevoked *UnrevokedError
				live := !revokeOK && !backlogOK
				if errors.As(err, &unrevoked) != live || !errors.Is(err, ErrStore) {
					t.Fatalf("%v", err)
				}
				if len(c.argv) != 0 {
					t.Fatal("an unrecorded replacement was delivered")
				}
				rec, _ := store.record(t, ctrA)
				wantTokens(t, "record", tokens(rec), map[string]State{"tok_1": Delivered})
				if !live && !slices.Contains(p.revoked, "tok_2") && !slices.Contains(pendingTokens(store.backlog(t)), "tok_2") {
					t.Fatal("replacement lost")
				}
			})
		}
	}
}

func TestTargetsErrorRedacted(t *testing.T) {
	p := &secretProvider{newProvider(t, "acme")}
	_, err := lifecycle(newStore(), p, nil).Acquire(bg, to(ctrA, newContainer()), "app", nil)
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
	got, err := lifecycle(store, newProvider(t), nil).Acquire(bg, to(ctrA, newContainer()), "app", nil)
	if err != nil || got == nil || len(got) != 0 || len(store.items) != 0 {
		t.Fatalf("%v %v %v", got, err, store.items)
	}
}

// TestHoldAndRetire is a token the broker itself uses once (GitHub's prefetch): held in its consumer's record while in
// use, revoked after, and never lost when a write or the revocation fails.
func TestHoldAndRetire(t *testing.T) {
	m := Material{Account: "acme", Target: 1, Token: "ghs_prefetch", ExpiresAt: clock.Add(time.Hour).Format(time.RFC3339)}
	store, p := newStore(), newProvider(t)
	l := lifecycle(store, p, nil)
	if err := l.Hold(bg, Consumer{Container: ctrA}, m); err != nil {
		t.Fatal(err)
	}
	rec, _ := store.record(t, ctrA)
	wantTokens(t, "held", tokens(rec), map[string]State{"ghs_prefetch": Prepared})
	l.Retire(bg, Consumer{Container: ctrA}, m)
	if _, ok := store.record(t, ctrA); ok || !slices.Equal(p.revoked, []string{"ghs_prefetch"}) {
		t.Fatalf("retire left the record or skipped the revocation: %q", p.revoked)
	}

	store, p = newStore(), newProvider(t)
	l = lifecycle(store, p, nil)
	store.fail = func(op, service string) bool { return service == records && op == "write" }
	if err := l.Hold(bg, Consumer{Container: ctrA}, m); !errors.Is(err, ErrStore) {
		t.Fatalf("hold with the record down: %v", err)
	}
	if !slices.Contains(p.revoked, "ghs_prefetch") && !slices.Contains(pendingTokens(store.backlog(t)), "ghs_prefetch") {
		t.Fatal("an unrecorded held token was lost")
	}

	store, p = newStore(), newProvider(t)
	l = lifecycle(store, p, nil)
	if err := l.Hold(bg, Consumer{Container: ctrA}, m); err != nil {
		t.Fatal(err)
	}
	p.revoke = failRevoke
	l.Retire(bg, Consumer{Container: ctrA}, m)
	if !slices.Equal(pendingTokens(store.backlog(t)), []string{"ghs_prefetch"}) {
		t.Fatalf("an unrevoked held token not queued: %v", store.backlog(t))
	}
}

func TestRollbackReportsUnrevokedWhenRemovalFails(t *testing.T) {
	store, p := newStore(), newProvider(t, "acme", "other", "third")
	writes := 0
	store.fail = func(op, service string) bool {
		if service == backlogS {
			return true
		}
		if service == records && op == "write" {
			writes++
			return writes >= 3
		}
		return false
	}
	p.revoke = func(token string) Revocation {
		if token != "tok_1" { // tok_2 keeps the record, so removing tok_1 is a failing write
			return Revocation{Outcome: Retryable, Err: errors.New("GitHub 503")}
		}
		return Revocation{Outcome: Revoked}
	}
	_, err := lifecycle(store, p, nil).Acquire(bg, to(ctrA, newContainer()), "app", nil)
	var unrevoked *UnrevokedError
	if !errors.As(err, &unrevoked) || !errors.Is(err, ErrStore) {
		t.Fatalf("a live, unqueued tok_3 reported as %v", err)
	}
}

func TestRefreshMarkWithoutReplacement(t *testing.T) {
	store, p := newStore(), newProvider(t, "acme")
	l := lifecycle(store, p, nil)
	acquire(t, l, ctrA, newContainer())
	before := slices.Clone(store.items[records+"/"+ctrA])
	c := newContainer()
	c.fail = func(int) bool {
		store.items[records+"/"+ctrA] = slices.Clone(before) // another command took the replacement out meanwhile
		return false
	}
	if _, err := l.Refresh(bg, to(ctrA, c), nil); !errors.Is(err, ErrReleased) {
		t.Fatalf("%v", err)
	}
	rec, _ := store.record(t, ctrA)
	wantTokens(t, "record", tokens(rec), map[string]State{"tok_1": Delivered})
	if slices.Contains(p.revoked, "tok_1") {
		t.Fatal("the live token was retired")
	}
}

func TestReleaseUnreadableRecordWithBacklogDown(t *testing.T) {
	store, p := newStore(), newProvider(t, "acme")
	l := lifecycle(store, p, nil)
	acquire(t, l, ctrA, newContainer())
	reads := 0
	store.fail = func(op, service string) bool {
		if service == records && op == "read" {
			reads++
			return reads >= 2 // Release's first read works; discard's fails
		}
		return service == backlogS
	}
	var unrevoked *UnrevokedError
	if err := l.Release(bg, Consumer{Container: ctrA}); !errors.As(err, &unrevoked) {
		t.Fatalf("release reported %v with its record unread and kept", err)
	}
	if _, ok := store.record(t, ctrA); !ok {
		t.Fatal("record lost")
	}
}

// TestRefreshUsesRecordedScope: a renewal never widens a grant; a record an older broker wrote, with no scope, renews
// with the caller's.
func TestRefreshUsesRecordedScope(t *testing.T) {
	recorded, caller := map[string]string{"contents": "read"}, map[string]string{"contents": "write"}
	store, p := newStore(), newProvider(t)
	store.seed(t, ctrA, Record{Subject: "app", Scope: recorded, Tokens: []Material{{Account: "acme", Target: 1, Token: "ghs_old", State: Delivered}}})
	store.seed(t, ctrB, Record{Subject: "app", Tokens: []Material{{Account: "acme", Target: 1, Token: "ghs_legacy"}}})
	l := lifecycle(store, p, nil)
	for _, id := range []string{ctrA, ctrB} {
		if _, err := l.Refresh(bg, to(id, newContainer()), caller); err != nil {
			t.Fatal(err)
		}
	}
	if len(p.scopes) != 2 || !maps.Equal(p.scopes[0], recorded) || !maps.Equal(p.scopes[1], caller) {
		t.Fatalf("scopes %v", p.scopes)
	}
}

// TestHoldRefusedStillRevokes: Hold gets a token already issued, so refusing it (no valid consumer, a shared provider)
// still revokes it, touching no store; a revocation that fails leaves an *UnrevokedError.
func TestHoldRefusedStillRevokes(t *testing.T) {
	m := Material{Account: "acme", Target: 1, Token: "ghs_prefetch", ExpiresAt: clock.Add(time.Hour).Format(time.RFC3339)}
	for name, tc := range map[string]struct {
		c      Consumer
		shared bool
	}{
		"no consumer":     {Consumer{}, false},
		"bad consumer":    {Consumer{Container: "a/b"}, false},
		"shared provider": {Consumer{Container: ctrA}, true},
	} {
		for _, revokeOK := range []bool{true, false} {
			store, fp := newStore(), newProvider(t)
			store.fail = func(op, service string) bool { t.Errorf("%s: store %s %s", name, op, service); return false }
			var p Provider = fp
			if tc.shared {
				p = sharedProvider{fp}
			}
			if !revokeOK {
				fp.revoke = failRevoke
			}
			err := lifecycle(store, p, nil).Hold(bg, tc.c, m)
			var unrevoked *UnrevokedError
			if err == nil || errors.As(err, &unrevoked) == revokeOK || fp.revokes() != 1 {
				t.Errorf("%s revoke=%v: %v, revokes %d", name, revokeOK, err, fp.revokes())
			}
			if revokeOK && !slices.Equal(fp.revoked, []string{"ghs_prefetch"}) {
				t.Errorf("%s: revoked %q", name, fp.revoked)
			}
		}
	}
}

// TestHoldAppendsToRecord: a held token joins whatever the record already has, after its highest revision.
func TestHoldAppendsToRecord(t *testing.T) {
	store, p := newStore(), newProvider(t)
	store.seed(t, ctrA, Record{Subject: "app", Tokens: []Material{
		{Account: "acme", Target: 1, Token: "ghs_live", Revision: 2, State: Delivered},
		{Account: "acme", Target: 1, Token: "ghs_held", Revision: 3, State: Prepared},
	}})
	l := lifecycle(store, p, nil)
	if err := l.Hold(bg, Consumer{Container: ctrA}, Material{Account: "acme", Target: 1, Token: "ghs_more"}); err != nil {
		t.Fatal(err)
	}
	rec, _ := store.record(t, ctrA)
	wantTokens(t, "record", tokens(rec), map[string]State{"ghs_live": Delivered, "ghs_held": Prepared, "ghs_more": Prepared})
	if last := rec.Tokens[len(rec.Tokens)-1]; last.Token != "ghs_more" || last.Revision != 4 {
		t.Fatalf("%+v", last)
	}
}

// TestEnderEndsOnce: an Ender issuer's run ends exactly once, with the run's error, and End's error joins the result.
func TestEnderEndsOnce(t *testing.T) {
	failing := newContainer()
	failing.fail = func(int) bool { return true }
	issueFails := errors.New("issue failed")
	for name, c := range map[string]struct {
		run    func(l Lifecycle, p *endingProvider) error
		failed bool
	}{
		"acquire": {run: func(l Lifecycle, _ *endingProvider) error {
			_, err := l.Acquire(bg, to(ctrA, newContainer()), "app", nil)
			return err
		}},
		"acquire issue failure": {failed: true, run: func(l Lifecycle, p *endingProvider) error {
			p.issueErr = func(int) error { return issueFails }
			_, err := l.Acquire(bg, to(ctrA, newContainer()), "app", nil)
			return err
		}},
		"acquire delivery failure": {failed: true, run: func(l Lifecycle, _ *endingProvider) error {
			_, err := l.Acquire(bg, to(ctrA, failing), "app", nil)
			return err
		}},
		"refresh": {run: func(l Lifecycle, p *endingProvider) error {
			acquire(t, l, ctrA, newContainer())
			p.ends = nil
			_, err := l.Refresh(bg, to(ctrA, newContainer()), nil)
			return err
		}},
		"refresh delivery failure": {failed: true, run: func(l Lifecycle, p *endingProvider) error {
			acquire(t, l, ctrA, newContainer())
			p.ends = nil
			_, err := l.Refresh(bg, to(ctrA, failing), nil)
			return err
		}},
	} {
		t.Run(name, func(t *testing.T) {
			p := &endingProvider{fakeProvider: newProvider(t, "acme")}
			err := c.run(lifecycle(newStore(), p, nil), p)
			if len(p.ends) != 1 || (p.ends[0] != nil) != c.failed || (err != nil) != c.failed {
				t.Fatalf("ends %v, run error %v", p.ends, err)
			}
			if c.failed && !errors.Is(err, p.ends[0]) && !errors.Is(p.ends[0], err) {
				t.Fatalf("End got %v, run returned %v", p.ends[0], err)
			}
		})
	}
	p := &endingProvider{fakeProvider: newProvider(t, "acme"), endErr: errors.New("end failed")}
	if _, err := lifecycle(newStore(), p, nil).Acquire(bg, to(ctrA, newContainer()), "app", nil); !errors.Is(err, p.endErr) {
		t.Fatalf("End's error not returned: %v", err)
	}
}

// TestWriterReplacesDeliver: a Writer issuer writes its material itself; the provider's File is never consulted.
func TestWriterReplacesDeliver(t *testing.T) {
	fp := newProvider(t, "bad/account") // File refuses it: only the Writer path can succeed
	l := lifecycle(newStore(), writingProvider{fp}, nil)
	c := newContainer()
	acquire(t, l, ctrA, c)
	if c.files["bad/account"] != "carrier-tok_1" || len(c.argv) != 1 || c.argv[0][0] != "write" {
		t.Fatalf("container %v %q", c.files, c.argv)
	}
	if _, err := l.Acquire(bg, Delivery{Consumer: Consumer{Host: "linear"}}, "app", nil); err != nil {
		t.Fatalf("host consumer: %v", err)
	}
}

// TestPublishBeforeDelivered: Publish runs once the material is in the container, before it counts as delivered; a
// failed publication leaves a shared provider's material prepared and retires a non-shared one's.
func TestPublishBeforeDelivered(t *testing.T) {
	store, fp := newStore(), newProvider(t, "acme")
	l := lifecycle(store, fp, nil)
	acquire(t, l, ctrA, newContainer())
	c := newContainer()
	var seen map[string]State
	d := to(ctrA, c)
	d.Publish = func(m Material) error {
		rec, _ := store.record(t, ctrA)
		seen = tokens(rec)
		if m.Token != "tok_2" || c.files["acme"] != "tok_2" {
			t.Errorf("published %+v before delivery %v", m, c.files)
		}
		return nil
	}
	if _, err := l.Refresh(bg, d, nil); err != nil {
		t.Fatal(err)
	}
	wantTokens(t, "record at publication", seen, map[string]State{"tok_1": Delivered, "tok_2": Prepared})

	published := errors.New("deadline write failed")
	d.Publish = func(Material) error { return published }
	if _, err := l.Refresh(bg, d, nil); !errors.Is(err, published) {
		t.Fatalf("%v", err)
	}
	rec, _ := store.record(t, ctrA)
	wantTokens(t, "non-shared after failed publication", tokens(rec), map[string]State{"tok_2": Delivered})
	if !slices.Contains(fp.revoked, "tok_3") {
		t.Fatalf("unpublished token not retired: %q", fp.revoked)
	}

	store, fp = newStore(), newProvider(t, "account")
	l = lifecycle(store, sharedProvider{fp}, nil)
	d = to(ctrA, newContainer())
	d.Publish = func(Material) error { return published }
	if _, err := l.Acquire(bg, d, "app", nil); !errors.Is(err, published) {
		t.Fatalf("%v", err)
	}
	rec, _ = store.record(t, ctrA)
	wantTokens(t, "shared after failed acquire publication", tokens(rec), map[string]State{"tok_1": Prepared})
	d.Publish = nil
	if _, err := l.Refresh(bg, d, nil); err != nil {
		t.Fatal(err)
	}
	d.Publish = func(Material) error { return published }
	if _, err := l.Refresh(bg, d, nil); !errors.Is(err, published) {
		t.Fatalf("%v", err)
	}
	rec, _ = store.record(t, ctrA)
	wantTokens(t, "shared after failed refresh publication", tokens(rec), map[string]State{"tok_2": Delivered, "tok_3": Prepared})
	if fp.revokes() != 0 {
		t.Fatalf("shared material revoked: %q", fp.calls)
	}
}

// TestSharedDeliveryFailureKeepsRecord: a shared provider's failed delivery keeps the new material prepared beside the
// usable one, in Acquire and in Refresh.
func TestSharedDeliveryFailureKeepsRecord(t *testing.T) {
	store, fp := newStore(), newProvider(t, "account")
	l := lifecycle(store, sharedProvider{fp}, nil)
	failing := newContainer()
	failing.fail = func(int) bool { return true }
	if _, err := l.Acquire(bg, to(ctrA, failing), "app", nil); !errors.Is(err, ErrDelivery) {
		t.Fatalf("%v", err)
	}
	rec, _ := store.record(t, ctrA)
	wantTokens(t, "after failed acquire", tokens(rec), map[string]State{"tok_1": Prepared})
	acquire(t, l, ctrB, newContainer())
	if _, err := l.Refresh(bg, to(ctrB, failing), nil); !errors.Is(err, ErrDelivery) {
		t.Fatalf("%v", err)
	}
	rec, _ = store.record(t, ctrB)
	wantTokens(t, "after failed refresh", tokens(rec), map[string]State{"tok_2": Delivered, "tok_3": Prepared})
	if fp.revokes() != 0 || len(store.backlog(t)) != 0 {
		t.Fatalf("shared material revoked: %q", fp.calls)
	}
}

// TestRefreshTargetIsNewestMaterial: Refresh gives the provider each slot's newest recorded material as its target.
func TestRefreshTargetIsNewestMaterial(t *testing.T) {
	store, fp := newStore(), newProvider(t, "acme")
	exp := clock.Add(time.Hour).Format(time.RFC3339)
	store.seed(t, ctrA, Record{Subject: "app", Tokens: []Material{
		{Account: "acme", Target: 1, Token: "tok_b", ExpiresAt: exp, Revision: 2, State: Prepared},
		{Account: "acme", Target: 1, Token: "tok_a", ExpiresAt: exp, Revision: 1, State: Delivered},
	}})
	if _, err := lifecycle(store, fp, nil).Refresh(bg, to(ctrA, newContainer()), nil); err != nil {
		t.Fatal(err)
	}
	want := Material{Account: "acme", Target: 1, Token: "tok_b", ExpiresAt: exp, Revision: 2, State: Prepared}
	if len(fp.targetsIssued) != 1 || fp.targetsIssued[0] != want {
		t.Fatalf("targets %+v", fp.targetsIssued)
	}
}

// TestRefreshReissuedTokenNotDuplicated: a provider that issues material the slot already holds (a shared account not
// yet renewed) gets that entry delivered anew, never a duplicate.
func TestRefreshReissuedTokenNotDuplicated(t *testing.T) {
	store, fp := newStore(), newProvider(t, "account")
	fp.fixed = "acct_1"
	l := lifecycle(store, sharedProvider{fp}, nil)
	acquire(t, l, ctrA, newContainer())
	if _, err := l.Refresh(bg, to(ctrA, newContainer()), nil); err != nil {
		t.Fatal(err)
	}
	rec, _ := store.record(t, ctrA)
	if len(rec.Tokens) != 1 || rec.Tokens[0].State != Delivered || rec.Tokens[0].Revision != 1 {
		t.Fatalf("record %+v", rec.Tokens)
	}
	failing := newContainer()
	failing.fail = func(int) bool { return true }
	if _, err := l.Refresh(bg, to(ctrA, failing), nil); err == nil {
		t.Fatal("refresh succeeded")
	}
	rec, _ = store.record(t, ctrA)
	if len(rec.Tokens) != 1 || rec.Tokens[0].State != Prepared {
		t.Fatalf("record after failed redelivery %+v", rec.Tokens)
	}
	acquire(t, l, ctrA, newContainer())
	rec, _ = store.record(t, ctrA)
	if len(rec.Tokens) != 1 || rec.Tokens[0].State != Delivered || rec.Tokens[0].Revision != 1 {
		t.Fatalf("record after a second acquire %+v", rec.Tokens)
	}
}
