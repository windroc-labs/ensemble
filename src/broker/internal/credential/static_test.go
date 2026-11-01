package credential

import "testing"

func TestStaticRefAndLogin(t *testing.T) {
	for _, c := range []struct {
		kind      Kind
		name, ref string
		login     string
	}{
		{Claude, "default", "ensemble-broker-claude/default", "ensemble-broker login claude default"},
		{Claude, "work", "ensemble-broker-claude/work", "ensemble-broker login claude work"},
		{Opencode, "strata", "ensemble-broker-opencode/strata", "ensemble-broker login opencode strata"},
		{Env, "db", "ensemble-broker-env/db", "ensemble-broker login env db"},
	} {
		if ref := StaticRef(c.kind, c.name); ref != c.ref || LoginCommand(ref) != c.login {
			t.Errorf("%s %s: %q %q", c.kind, c.name, ref, LoginCommand(ref))
		}
	}
	defer func() {
		if recover() == nil {
			t.Fatal("a token kind made a static reference")
		}
	}()
	StaticRef(GitHub, "x")
}
