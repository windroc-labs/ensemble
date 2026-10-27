package launcher

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
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

func TestStaticStatus(t *testing.T) {
	h, _ := statusHost(t)
	got, err := h.StaticStatus(false, map[string]bool{"ensemble-broker-claude/default": true})
	if err != nil {
		t.Fatal(err)
	}
	encoded, _ := json.Marshal(got)
	want := `[{"reference":"ensemble-broker-claude/default","version":0,"expires_at":null,"provenance":"unknown","notice":"` + credential.UnknownText("ensemble-broker-claude/default") + `"},` +
		`{"reference":"ensemble-broker-claude/work","version":1,"expires_at":"2026-10-31T12:00:00Z","provenance":"explicit","notice":"ensemble-broker-claude/work expires 2026-10-31 (explicit), in 5 days: run claude setup-token, then ensemble-broker login claude work --fresh-token; running sessions keep their copy until they restart"},` +
		`{"reference":"ensemble-broker-env/db","version":1,"expires_at":null,"provenance":"unknown","notice":null},` +
		`{"reference":"ensemble-broker-opencode/strata","version":1,"expires_at":null,"provenance":"unknown","notice":null}]`
	if os.Getuid() != 0 && string(encoded) != want {
		t.Fatalf("%s\nwant\n%s", encoded, want)
	}
	if got, err := h.StaticStatus(false, map[string]bool{"ensemble-broker-claude/default": false}); err != nil || len(got) != 3 {
		t.Fatalf("an unusable legacy token is not listed: %+v %v", got, err)
	}
	if got, err := (Host{}).StaticStatus(false, nil); err != nil || got != nil {
		t.Fatalf("no store: %+v %v", got, err)
	}
}

func TestStaticStatusReadOnly(t *testing.T) {
	h, store := statusHost(t)
	before, _ := os.ReadFile(store.Path)
	_, logPath := audited(t, &h, "status", "")
	if _, err := h.StaticStatus(false, nil); err != nil {
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

func TestStatusCleanupNotifiesOnce(t *testing.T) {
	h, store := statusHost(t)
	_, logPath := audited(t, &h, "status", "")
	for range 2 {
		if _, err := h.StaticStatus(true, nil); err != nil {
			t.Fatal(err)
		}
	}
	login(t, store, "ensemble-broker-env/later", credential.Registration{Expires: noticeNow.Add(24 * time.Hour)}, noticeNow)
	if err := store.DeliverHost("ensemble-broker-env/later", noticeNow, func(credential.Notice) {}); err != nil { // as login does
		t.Fatal(err)
	}
	if _, err := h.StaticStatus(true, nil); err != nil {
		t.Fatal(err)
	}
	if err := h.Audit.Flush(nil); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(logPath)
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(strings.TrimSpace(string(data)), "\n")
	if len(lines) != 1 || !strings.Contains(lines[0], `"command":"status","event":"credential-notice"`) ||
		!strings.Contains(lines[0], `"item":"ensemble-broker-claude/work","notice":"7d"`) {
		t.Fatalf("%q", lines)
	}
	records, _ := store.Load()
	if n := records["ensemble-broker-claude/work"].Notified; len(n) != 1 || n[0] != "7d" {
		t.Fatalf("%q", n)
	}
}

func TestStaticStatusStoreFails(t *testing.T) {
	h := Host{Static: &fakeStatic{err: &credential.StoreError{Path: "/x/static-secrets.json", Reason: "cannot read"}}}
	_, err := h.StaticStatus(false, nil)
	if e := protoErr(t, err); e.Code != protocol.InternalError || e.Message != "/x/static-secrets.json: cannot read." {
		t.Fatalf("%+v", e)
	}
}
