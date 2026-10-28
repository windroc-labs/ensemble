package credential

import (
	"slices"
	"testing"

	"ensemble/broker/internal/protocol"
)

// TestHostConsumerLinearContract is Linear's shape: a host identity with no container, holding a shared account's
// token, which a refresh withdraws from its record, never revokes.
func TestHostConsumerLinearContract(t *testing.T) {
	store, p := newStore(), newProvider(t, "workspace")
	l := lifecycle(store, p, nil)
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
	rec, _ = store.record(t, "host/linear-app")
	wantTokens(t, "after refresh", tokens(rec), map[string]State{"tok_2": Delivered})
}
