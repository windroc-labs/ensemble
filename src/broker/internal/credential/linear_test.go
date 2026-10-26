package credential

import (
	"slices"
	"testing"
	"time"

	"ensemble/broker/internal/protocol"
)

// TestHostConsumerLinearContract is the reserved Linear shape (FR-5, FR-18): a host identity with no container, whose
// app token the provider cannot revoke. No production code issues Linear tokens.
func TestHostConsumerLinearContract(t *testing.T) {
	store, p := newStore(), newProvider(t, "workspace")
	p.kind = Linear
	p.revoke = func(string) Revocation { return Revocation{Outcome: Unsupported} }
	l, err := New(lifecycle(store, p, nil))
	if err != nil {
		t.Fatal(err)
	}
	host := Delivery{Consumer: Consumer{Host: "linear-app"}}
	got, err := l.Acquire(bg, host, "linear-app", map[string]string{"scope": "read"})
	if err != nil {
		t.Fatal(err)
	}
	if want := []protocol.Expiry{{Account: "workspace", ExpiresAt: "2026-10-26T13:00:00Z"}}; !slices.Equal(got, want) {
		t.Fatalf("expiries %v", got)
	}
	rec, ok := store.record(t, "host/linear-app")
	if !ok || rec.Tokens[0].State != Delivered {
		t.Fatalf("record %+v", rec)
	}
	if got, err = l.Refresh(bg, host, nil); err != nil || len(got) != 1 {
		t.Fatalf("refresh %v %v", got, err)
	}
	if err := l.Release(bg, host.Consumer); err != nil {
		t.Fatal(err)
	}
	pending, err := l.Pending()
	if err != nil || len(pending) != 2 || pending[0].Container != "host/linear-app" {
		t.Fatalf("pending %v %v", pending, err)
	}
	if left, err := l.Retry(bg); err != nil || len(left) != 2 {
		t.Fatalf("unsupported revocation dropped before expiry: %v %v", left, err)
	}
	p.now = p.now.Add(2 * time.Hour)
	if left, err := l.Retry(bg); err != nil || len(left) != 0 {
		t.Fatalf("expired entries kept: %v %v", left, err)
	}
	if len(p.revoked) != 0 {
		t.Fatalf("unsupported counted as revoked: %q", p.revoked)
	}
}
