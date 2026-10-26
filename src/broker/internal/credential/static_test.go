package credential

import (
	"strings"
	"testing"
	"time"

	"ensemble/broker/internal/audit"
)

var staticNow = time.Date(2026, 10, 26, 12, 0, 0, 0, time.UTC)

func date(t *testing.T, s string) time.Time {
	t.Helper()
	d, err := ParseDate(s)
	if err != nil {
		t.Fatal(err)
	}
	return d
}

func TestParseDate(t *testing.T) {
	if d, err := ParseDate("2027-03-04"); err != nil || !d.Equal(time.Date(2027, 3, 4, 0, 0, 0, 0, time.UTC)) {
		t.Fatalf("%v %v", d, err)
	}
	for _, bad := range []string{"", "2027-3-4", "2027-02-30", "2027-03-04T00:00:00Z", "tomorrow", " 2027-03-04"} {
		if _, err := ParseDate(bad); err == nil {
			t.Errorf("%q parsed", bad)
		}
	}
}

func TestResolve(t *testing.T) {
	for _, c := range []struct {
		name    string
		kind    Kind
		reg     Registration
		expires string
		p       Provenance
		err     string
	}{
		{"none", Claude, Registration{}, "", Unknown, ""},
		{"fresh", Claude, Registration{Fresh: true}, "2027-10-26T12:00:00Z", Assumed, ""},
		{"expires", Claude, Registration{Expires: date(t, "2027-01-01")}, "2027-01-01T00:00:00Z", Explicit, ""},
		{"issued", Claude, Registration{Issued: date(t, "2026-02-01")}, "2027-02-01T00:00:00Z", Derived, ""},
		{"issued today", Claude, Registration{Issued: date(t, "2026-10-26")}, "2027-10-26T00:00:00Z", Derived, ""},
		{"both agree", Claude, Registration{Issued: date(t, "2026-02-01"), Expires: date(t, "2027-02-01")}, "2027-02-01T00:00:00Z", Explicit, ""},
		{"both disagree", Claude, Registration{Issued: date(t, "2026-02-01"), Expires: date(t, "2027-03-01")}, "", "", "--issued 2026-02-01 means expiry 2027-02-01, not 2027-03-01"},
		{"fresh and expires", Claude, Registration{Fresh: true, Expires: date(t, "2027-01-01")}, "", "", "--fresh-token takes no date"},
		{"fresh and issued", Claude, Registration{Fresh: true, Issued: date(t, "2026-01-01")}, "", "", "--fresh-token takes no date"},
		{"issued future", Claude, Registration{Issued: date(t, "2026-10-27")}, "", "", "--issued 2026-10-27 is in the future"},
		{"expires today", Claude, Registration{Expires: date(t, "2026-10-26")}, "", "", "2026-10-26 is not in the future: register a new secret"},
		{"derived past", Claude, Registration{Issued: date(t, "2025-10-01")}, "", "", "2026-10-01 is not in the future: register a new secret"},
		{"env expires", Env, Registration{Expires: date(t, "2027-01-01")}, "2027-01-01T00:00:00Z", Explicit, ""},
		{"env none", Env, Registration{}, "", Unknown, ""},
		{"opencode expires", Opencode, Registration{Expires: date(t, "2027-01-01")}, "2027-01-01T00:00:00Z", Explicit, ""},
		{"env fresh", Env, Registration{Fresh: true}, "", "", "--issued and --fresh-token apply only to login claude"},
		{"opencode issued", Opencode, Registration{Issued: date(t, "2026-01-01")}, "", "", "--issued and --fresh-token apply only to login claude"},
	} {
		expires, p, err := c.reg.Resolve(c.kind, staticNow)
		switch {
		case c.err != "" && (err == nil || err.Error() != c.err):
			t.Errorf("%s: err %v, want %q", c.name, err, c.err)
		case c.err == "" && (err != nil || expires != c.expires || p != c.p):
			t.Errorf("%s: %q %q %v, want %q %q", c.name, expires, p, err, c.expires, c.p)
		}
	}
}

func TestStaticRefAndLogin(t *testing.T) {
	for _, c := range []struct {
		kind      Kind
		name, ref string
		login     string
	}{
		{Claude, "default", "ensemble-claude/default", "ensemble-broker login claude"},
		{Claude, "work", "ensemble-claude/work", "ensemble-broker login claude work"},
		{Opencode, "strata", "ensemble-opencode/strata", "ensemble-broker login opencode strata"},
		{Env, "db", "ensemble-env/db", "ensemble-broker login env db"},
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

func expiring(version int, expires string) Static {
	return Static{Version: version, ExpiresAt: expires, Provenance: Explicit}
}

func keys(notices []Notice) []string {
	var out []string
	for _, n := range notices {
		out = append(out, n.Key)
	}
	return out
}

func none(string) bool { return false }

func TestDueThresholds(t *testing.T) {
	const ref = "ensemble-claude/default"
	s := expiring(3, "2026-12-01T00:00:00Z")
	for _, c := range []struct {
		at   time.Time
		want string
	}{
		{time.Date(2026, 10, 31, 23, 59, 59, 0, time.UTC), ""},
		{time.Date(2026, 11, 1, 0, 0, 0, 0, time.UTC), ref + "@3:30d"},
		{time.Date(2026, 11, 24, 0, 0, 0, 0, time.UTC), ref + "@3:7d"},
		{time.Date(2026, 11, 30, 0, 0, 0, 0, time.UTC), ref + "@3:1d"},
		{time.Date(2026, 12, 1, 0, 0, 0, 0, time.UTC), ref + "@3:expired"},
		{time.Date(2027, 6, 1, 0, 0, 0, 0, time.UTC), ref + "@3:expired"},
	} {
		got := strings.Join(keys(Due(ref, s, s, none, c.at)), ",")
		if got != c.want {
			t.Errorf("%v: %q, want %q", c.at, got, c.want)
		}
	}
	at := time.Date(2026, 11, 25, 0, 0, 0, 0, time.UTC) // 30d and 7d reached
	delivered := func(set ...string) func(string) bool {
		return func(k string) bool {
			for _, s := range set {
				if s == k {
					return true
				}
			}
			return false
		}
	}
	if got := keys(Due(ref, s, s, delivered(ref+"@3:30d"), at)); len(got) != 1 || got[0] != ref+"@3:7d" {
		t.Errorf("30d delivered: %q", got)
	}
	if got := keys(Due(ref, s, s, delivered(ref+"@3:7d"), at)); len(got) != 0 {
		t.Errorf("7d delivered: %q", got)
	}
	if got := keys(Due(ref, s, s, delivered(ref+"@3:expired"), at)); len(got) != 0 {
		t.Errorf("a more urgent one delivered: %q", got)
	}
	next := expiring(4, "2026-12-01T00:00:00Z")
	if got := keys(Due(ref, next, next, delivered(ref+"@3:7d"), at)); len(got) != 1 || got[0] != ref+"@4:7d" {
		t.Errorf("new version: %q", got)
	}
	unknown := Static{Version: 2, Provenance: Unknown}
	if got := Due(ref, unknown, unknown, none, at); len(got) != 0 {
		t.Errorf("unknown: %q", keys(got))
	}
	pending := Static{Version: 2, ExpiresAt: "2026-11-01T00:00:00Z", Provenance: Explicit, Pending: true}
	if got := Due(ref, pending, pending, none, at); len(got) != 0 {
		t.Errorf("pending: %q", keys(got))
	}
}

func TestDueRotated(t *testing.T) {
	const ref = "ensemble-env/db"
	injected := Static{Version: 1, Provenance: Unknown}
	stored := Static{Version: 2, Provenance: Unknown}
	got := Due(ref, injected, stored, none, staticNow)
	if len(got) != 1 || got[0].Key != ref+"@2:rotated" || got[0].Text != "ensemble-env/db was replaced (version 2); this session holds version 1 until it restarts" {
		t.Fatalf("%+v", got)
	}
	if got := Due(ref, injected, stored, func(k string) bool { return k == ref+"@2:rotated" }, staticNow); len(got) != 0 {
		t.Errorf("delivered: %+v", got)
	}
	stored.Pending = true
	if got := Due(ref, injected, stored, none, staticNow); len(got) != 0 {
		t.Errorf("pending stored: %+v", got)
	}
	if got := Due(ref, injected, injected, none, staticNow); len(got) != 0 {
		t.Errorf("same version: %+v", got)
	}
	old := expiring(1, "2026-11-01T00:00:00Z")
	if got := keys(Due(ref, old, stored.withPending(false), none, staticNow)); strings.Join(got, ",") != ref+"@1:7d,"+ref+"@2:rotated" {
		t.Errorf("expiring and replaced: %q", got)
	}
}

func (s Static) withPending(p bool) Static {
	s.Pending = p
	return s
}

func TestNextCheck(t *testing.T) {
	if NextCheck(nil, staticNow) != nil {
		t.Fatal("no records")
	}
	unknown := map[string]Static{"ensemble-env/a": {Version: 1, Provenance: Unknown}}
	if got := NextCheck(unknown, staticNow); got == nil || !got.Equal(staticNow.Add(time.Hour)) {
		t.Fatalf("rotation poll: %v", got)
	}
	soon := map[string]Static{"ensemble-env/a": expiring(1, "2026-11-25T12:20:00Z"), "ensemble-env/b": {Version: 1, Provenance: Unknown}}
	if got := NextCheck(soon, staticNow); got == nil || !got.Equal(time.Date(2026, 10, 26, 12, 20, 0, 0, time.UTC)) {
		t.Fatalf("30d crossing: %v", got)
	}
	expired := map[string]Static{"ensemble-env/a": expiring(1, "2026-01-01T00:00:00Z")}
	if got := NextCheck(expired, staticNow); got == nil || !got.Equal(staticNow.Add(time.Hour)) {
		t.Fatalf("all past: %v", got)
	}
	odd := staticNow.Add(500 * time.Millisecond)
	if got := NextCheck(unknown, odd); got == nil || !got.Equal(staticNow.Add(time.Hour+time.Second)) {
		t.Fatalf("whole seconds, rounded up: %v", got)
	}
}

func TestText(t *testing.T) {
	s := Static{Version: 1, ExpiresAt: "2026-11-15T12:00:00Z", Provenance: Explicit}
	for _, c := range []struct {
		ref, threshold string
		at             time.Time
		want           string
	}{
		{"ensemble-claude/default", "30d", staticNow, "ensemble-claude/default expires 2026-11-15 (explicit), in 20 days: run claude setup-token, then ensemble-broker login claude --fresh-token; running sessions keep their copy until they restart"},
		{"ensemble-claude/work", "1d", time.Date(2026, 11, 14, 13, 0, 0, 0, time.UTC), "ensemble-claude/work expires 2026-11-15 (explicit), in 1 day: run claude setup-token, then ensemble-broker login claude work --fresh-token; running sessions keep their copy until they restart"},
		{"ensemble-opencode/strata", "expired", time.Date(2026, 11, 16, 0, 0, 0, 0, time.UTC), "ensemble-opencode/strata expired 2026-11-15 (explicit): issue a new key, then ensemble-broker login opencode strata --expires <date>; new starts are refused until then"},
		{"ensemble-env/db", "7d", time.Date(2026, 11, 10, 0, 0, 0, 0, time.UTC), "ensemble-env/db expires 2026-11-15 (explicit), in 6 days: rotate it at its source, then ensemble-broker login env db [--expires <date>]; running sessions keep their copy until they restart"},
	} {
		if got := Text(c.ref, s, c.threshold, c.at); got != c.want {
			t.Errorf("%s %s:\n%q\nwant\n%q", c.ref, c.threshold, got, c.want)
		}
	}
	if got := UnknownText("ensemble-claude/work"); got != "ensemble-claude/work expiry unknown: a date is recorded only with the token; after claude setup-token, rerun ensemble-broker login claude work --fresh-token, or paste the current token with --expires or --issued" {
		t.Errorf("unknown: %q", got)
	}
}

func TestStaticExpired(t *testing.T) {
	for _, c := range []struct {
		s    Static
		want bool
	}{
		{expiring(1, "2026-10-26T12:00:00Z"), true},
		{expiring(1, "2026-10-26T12:00:01Z"), false},
		{Static{Version: 1, Provenance: Unknown}, false},
		{Static{Version: 1, ExpiresAt: "2026-01-01T00:00:00Z", Provenance: Explicit, Pending: true}, false},
		{expiring(1, "not a time"), false},
	} {
		if got := c.s.Expired(staticNow); got != c.want {
			t.Errorf("%+v: %v", c.s, got)
		}
	}
}

func TestNoticeAudit(t *testing.T) {
	s := expiring(2, "2026-11-01T00:00:00Z")
	expiry := Due("ensemble-claude/default", s, s, none, staticNow)[0].Audit()
	if expiry.Kind != audit.ClaudeSetupToken || expiry.Item != "ensemble-claude/default" || expiry.Result != audit.Notified ||
		expiry.Notice != "7d" || expiry.ExpiresAt == nil || *expiry.ExpiresAt != "2026-11-01T00:00:00Z" {
		t.Fatalf("%+v", expiry)
	}
	rotated := Due("ensemble-opencode/strata", Static{Version: 1, Provenance: Unknown}, Static{Version: 2, Provenance: Unknown}, none, staticNow)[0].Audit()
	if rotated.Kind != audit.OpencodeAPIKey || rotated.Notice != "rotated" || rotated.ExpiresAt != nil {
		t.Fatalf("%+v", rotated)
	}
	if env := (Notice{Reference: "ensemble-env/db", Label: "1d"}).Audit(); env.Kind != audit.EnvSecret {
		t.Fatalf("%+v", env)
	}
}

func TestNoticeKeyPatternMatchesDue(t *testing.T) {
	s := expiring(7, "2026-12-01T00:00:00Z")
	newer := Static{Version: 8, Provenance: Unknown}
	for _, at := range []time.Time{time.Date(2026, 11, 1, 0, 0, 0, 0, time.UTC), time.Date(2026, 11, 24, 0, 0, 0, 0, time.UTC),
		time.Date(2026, 11, 30, 0, 0, 0, 0, time.UTC), time.Date(2026, 12, 1, 0, 0, 0, 0, time.UTC)} {
		for _, n := range Due("ensemble-opencode/strata", s, newer, none, at) {
			if !NoticeKeyPattern.MatchString(n.Key) {
				t.Errorf("%q", n.Key)
			}
		}
	}
	for _, bad := range []string{"ensemble-bot/x@1:30d", "ensemble-env/db@1:2d", "ensemble-env/db:30d", "ensemble-env/@1:30d"} {
		if NoticeKeyPattern.MatchString(bad) {
			t.Errorf("%q matched", bad)
		}
	}
}
