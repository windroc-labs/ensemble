package credential

import (
	"encoding/json"
	"errors"
	"testing"
	"time"
)

func TestRenewableRejectsStaticKinds(t *testing.T) {
	for _, k := range []Kind{GitHub, Codex, Linear} {
		if err := Renewable(k); err != nil {
			t.Errorf("%s: %v", k, err)
		}
	}
	for _, k := range []Kind{Claude, Opencode, Env} {
		if err := Renewable(k); !errors.Is(err, ErrStatic) {
			t.Errorf("%s: %v, want ErrStatic", k, err)
		}
	}
	if err := Renewable("gitlab"); err == nil || errors.Is(err, ErrStatic) {
		t.Errorf("unknown kind: %v", err)
	}
}

func TestMaterialLegacyStateIsEmpty(t *testing.T) {
	var rec Record
	legacy := `{"app":"ensemble-bot","tokens":[{"account":"acme","installation":7,"token":"ghs_x","expires_at":"2026-01-01T00:00:00Z"}]}`
	if err := json.Unmarshal([]byte(legacy), &rec); err != nil {
		t.Fatal(err)
	}
	m := rec.Tokens[0]
	if rec.Subject != "ensemble-bot" || m.Account != "acme" || m.Target != 7 || m.Token != "ghs_x" || m.State != "" || m.Revision != 0 {
		t.Fatalf("%+v", rec)
	}
	m.State = Prepared
	data, err := json.Marshal(m)
	if err != nil {
		t.Fatal(err)
	}
	var back Material
	if err := json.Unmarshal(data, &back); err != nil || back.State != Prepared {
		t.Fatalf("state did not round-trip: %s", data)
	}
}

func TestExpired(t *testing.T) {
	now := time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC)
	for expires, want := range map[string]bool{
		"2026-01-01T11:59:59Z": true,
		"2026-01-01T12:00:00Z": true,
		"2026-01-01T12:00:01Z": false,
		"":                     false,
		"soon":                 false,
	} {
		if got := (Material{ExpiresAt: expires}).Expired(now); got != want {
			t.Errorf("%q: %v", expires, got)
		}
	}
}

func TestRedact(t *testing.T) {
	if got := Redact("bad ghs_a and jwt.x.y, ghs_a again", "ghs_a", "", "jwt.x.y"); got != "bad [redacted] and [redacted], [redacted] again" {
		t.Fatal(got)
	}
	if got := Redact("nothing secret"); got != "nothing secret" {
		t.Fatal(got)
	}
}

func TestConsumerCheck(t *testing.T) {
	exec := func([]string, []byte) error { return nil }
	for _, c := range []struct {
		name string
		d    Delivery
		ok   bool
		id   string
	}{
		{"container", Delivery{Consumer: Consumer{Container: "ensemble-s-0123"}, Exec: exec}, true, "ensemble-s-0123"},
		{"host", Delivery{Consumer: Consumer{Host: "linear-app"}, Exec: nil}, true, "host/linear-app"},
		{"neither", Delivery{Consumer: Consumer{}, Exec: exec}, false, ""},
		{"both", Delivery{Consumer: Consumer{Container: "c", Host: "h"}, Exec: exec}, false, ""},
		{"container without exec", Delivery{Consumer: Consumer{Container: "c"}, Exec: nil}, false, ""},
		{"host with exec", Delivery{Consumer: Consumer{Host: "h"}, Exec: exec}, false, ""},
		{"container naming a host key", Delivery{Consumer: Consumer{Container: "host/linear-app"}, Exec: exec}, false, ""},
		{"host with slash", Delivery{Consumer: Consumer{Host: "a/b"}, Exec: nil}, false, ""},
	} {
		err := c.d.check()
		if (err == nil) != c.ok || err != nil && !errors.Is(err, ErrConsumer) {
			t.Errorf("%s: %v", c.name, err)
		}
		if c.ok && c.d.Consumer.ID() != c.id {
			t.Errorf("%s: id %q", c.name, c.d.Consumer.ID())
		}
	}
	if (Consumer{Container: "a/b"}).check() == nil || (Consumer{Host: "h"}).check() != nil {
		t.Fatal("consumer check")
	}
}
