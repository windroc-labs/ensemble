package credential

import (
	"errors"
	"testing"
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

func TestRedact(t *testing.T) {
	if got := Redact("bad ghs_a and jwt.x.y, ghs_a again", "ghs_a", "", "jwt.x.y"); got != "bad [redacted] and [redacted], [redacted] again" {
		t.Fatal(got)
	}
	if got := Redact("nothing secret"); got != "nothing secret" {
		t.Fatal(got)
	}
}

func TestConsumerCheck(t *testing.T) {
	for host, ok := range map[string]bool{"linear-app": true, "": false, "a/b": false} {
		c := Consumer{Host: host}
		if err := c.check(); (err == nil) != ok || err != nil && !errors.Is(err, ErrConsumer) {
			t.Errorf("%q: %v", host, err)
		}
	}
	if id := (Consumer{Host: "linear-app"}).ID(); id != "host/linear-app" {
		t.Fatalf("id %q", id)
	}
}
