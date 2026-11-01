package launcher

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"ensemble/broker/internal/credential"
	"ensemble/broker/internal/protocol"
)

// statusHost is a status host over a real store in a temporary home, holding a Claude token expiring in 5 days, an
// env secret of unknown expiry and an OpenCode key whose login is still pending.
func statusHost(t *testing.T) (Host, credential.StaticStore) {
	t.Helper()
	store := credential.StaticStore{Path: filepath.Join(t.TempDir(), "static-secrets.json")}
	login(t, store, "ensemble-broker-claude/work", credential.Registration{Expires: noticeNow.Add(5 * 24 * time.Hour)}, noticeNow)
	login(t, store, "ensemble-broker-env/db", credential.Registration{}, noticeNow)
	if _, err := store.Register("ensemble-broker-opencode/strata", credential.Registration{}, noticeNow, func() error {
		return os.Chmod(filepath.Dir(store.Path), 0o500) // the commit fails: the record stays pending
	}); err == nil && os.Getuid() != 0 {
		t.Fatal("commit did not fail")
	}
	if err := os.Chmod(filepath.Dir(store.Path), 0o700); err != nil {
		t.Fatal(err)
	}
	return Host{Static: store, Now: func() time.Time { return noticeNow }}, store
}

// usability is a Claude usability check that records each account it reads.
func usability(read *[]string, usable map[string]bool) func(string) bool {
	return func(account string) bool {
		*read = append(*read, account)
		return usable[account]
	}
}

func TestStatus(t *testing.T) {
	h, store := statusHost(t)
	login(t, store, "ensemble-broker-claude/home", credential.Registration{}, noticeNow)
	var read []string
	got, err := h.Status(usability(&read, map[string]bool{"work": true}))
	if err != nil {
		t.Fatal(err)
	}
	encoded, _ := json.Marshal(got)
	want := `{"claude":{"home":false,"work":true},"static":[` +
		`{"reference":"ensemble-broker-claude/home","version":1,"expires_at":null,"provenance":"unknown","notice":"` + credential.UnknownText("ensemble-broker-claude/home") + `"},` +
		`{"reference":"ensemble-broker-claude/work","version":1,"expires_at":"2026-10-31T12:00:00Z","provenance":"explicit","notice":"ensemble-broker-claude/work expires 2026-10-31 (explicit), in 5 days: run claude setup-token, then ensemble-broker login claude work --fresh-token; running sessions keep their copy until they restart"},` +
		`{"reference":"ensemble-broker-env/db","version":1,"expires_at":null,"provenance":"unknown","notice":null},` +
		`{"reference":"ensemble-broker-opencode/strata","version":1,"expires_at":null,"provenance":"unknown","notice":null}]}`
	if os.Getuid() != 0 && string(encoded) != want {
		t.Fatalf("%s\nwant\n%s", encoded, want)
	}
	slices.Sort(read)
	if !slices.Equal(read, []string{"home", "work"}) {
		t.Fatalf("read %q", read)
	}
}

// Status names only the Claude accounts logged in by name: none recorded is an empty map, and no default is read
// or added unless an account is named default.
func TestStatusNamedClaudeAccountsOnly(t *testing.T) {
	for _, c := range []struct {
		accounts []string
		want     string
	}{
		{nil, `{"claude":{}}`},
		{[]string{"work"}, `{"claude":{"work":true}`},
		{[]string{"work", "home"}, `{"claude":{"home":true,"work":true}`},
		{[]string{"default"}, `{"claude":{"default":true}`},
	} {
		store := credential.StaticStore{Path: filepath.Join(t.TempDir(), "static-secrets.json")}
		for _, account := range c.accounts {
			login(t, store, "ensemble-broker-claude/"+account, credential.Registration{Expires: noticeNow.AddDate(1, 0, 0)}, noticeNow)
		}
		var read []string
		got, err := Host{Static: store, Now: func() time.Time { return noticeNow }}.Status(usability(&read, map[string]bool{"work": true, "home": true, "default": true}))
		if err != nil {
			t.Fatal(err)
		}
		encoded, _ := json.Marshal(got)
		slices.Sort(read)
		want := slices.Sorted(slices.Values(c.accounts))
		if !strings.HasPrefix(string(encoded), c.want) || !slices.Equal(read, want) || len(got.Static) != len(c.accounts) {
			t.Fatalf("%q: %s read %q", c.accounts, encoded, read)
		}
		for _, s := range got.Static {
			if s.Notice != nil && !strings.Contains(*s.Notice, "ensemble-broker login claude "+strings.TrimPrefix(s.Reference, "ensemble-broker-claude/")) {
				t.Fatalf("%q: notice %q", c.accounts, *s.Notice)
			}
		}
	}
	if got, err := (Host{}).Status(func(string) bool { t.Fatal("read without a store"); return false }); err != nil || got.Claude == nil || len(got.Claude) != 0 || got.Static != nil {
		t.Fatalf("no store: %+v %v", got, err)
	}
}

func TestStaticStatusReadOnly(t *testing.T) {
	h, store := statusHost(t)
	before, _ := os.ReadFile(store.Path)
	_, logPath := audited(t, &h, "status", "")
	if _, err := h.Status(func(string) bool { return true }); err != nil {
		t.Fatal(err)
	}
	if err := h.Audit.Flush(nil); err != nil {
		t.Fatal(err)
	}
	after, _ := os.ReadFile(store.Path)
	if !bytes.Equal(before, after) {
		t.Fatalf("status wrote the store:\n%s\n%s", before, after)
	}
	if _, err := os.Stat(logPath); !os.IsNotExist(err) {
		t.Fatalf("status audited: %v", err)
	}
}

func TestStaticStatusStoreFails(t *testing.T) {
	h := Host{Static: &fakeStatic{err: &credential.StoreError{Path: "/x/static-secrets.json", Reason: "cannot read"}}}
	_, err := h.Status(func(string) bool { return true })
	if e := protoErr(t, err); e.Code != protocol.InternalError || e.Message != "/x/static-secrets.json: cannot read." {
		t.Fatalf("%+v", e)
	}
}
