package launcher

import (
	"context"
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

var noticeNow = time.Date(2026, 10, 26, 12, 0, 0, 0, time.UTC)

// noticeHost is a started Claude container (env secrets dummy and ssh) whose static records are a real store in its
// temporary home, registered by register first; the clock is *now.
func noticeHost(t *testing.T, now *time.Time, register func(credential.StaticStore)) (Host, string, credential.StaticStore, string) {
	t.Helper()
	h, path, record, _ := staticStartHost(t, nil)
	store := credential.StaticStore{Path: filepath.Join(h.Home, ".ensemble/docker/static-secrets.json")}
	if register != nil {
		register(store)
	}
	h.Static = store
	h.Now = func() time.Time { return *now }
	if _, err := h.Start(context.Background(), path); err != nil {
		t.Fatal(err)
	}
	return h, path, store, record
}

func login(t *testing.T, s credential.StaticStore, ref string, reg credential.Registration, at time.Time) {
	t.Helper()
	if _, err := s.Register(ref, reg, at, func() error { return nil }); err != nil {
		t.Fatal(err)
	}
}

func noticeKeys(r MaintainResult) []string {
	var out []string
	for _, n := range r.Notices {
		out = append(out, n.Key)
	}
	return out
}

func TestNoticesThirtyDays(t *testing.T) {
	now := noticeNow
	h, path, _, _ := noticeHost(t, &now, func(s credential.StaticStore) {
		login(t, s, "ensemble-broker-claude/work", credential.Registration{Expires: noticeNow.Add(20 * 24 * time.Hour)}, noticeNow)
	})
	got, err := h.Maintain(context.Background(), container, path, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	want := "ensemble-broker-claude/work expires 2026-11-15 (explicit), in 20 days: run claude setup-token, then ensemble-broker login claude work --fresh-token; running sessions keep their copy until they restart"
	if len(got.Notices) != 1 || got.Notices[0].Key != "ensemble-broker-claude/work@1:30d" || got.Notices[0].Text != want {
		t.Fatalf("%+v", got)
	}
	if got.Container != container || got.NextCheck == nil || *got.NextCheck != "2026-10-26T13:00:00Z" {
		t.Fatalf("%+v", got)
	}
	encoded, _ := json.Marshal(got)
	if !strings.HasPrefix(string(encoded), `{"container":"`+container+`","next_check":"2026-10-26T13:00:00Z","deadline":null,"results":[],"notices":[{"key":"ensemble-broker-claude/work@1:30d","text":`) {
		t.Fatalf("%s", encoded)
	}
}

func TestNoticesDeliveredDedup(t *testing.T) {
	now := noticeNow
	h, path, _, record := noticeHost(t, &now, func(s credential.StaticStore) {
		login(t, s, "ensemble-broker-env/dummy", credential.Registration{Expires: time.Date(2026, 12, 1, 0, 0, 0, 0, time.UTC)}, noticeNow)
	})
	_, logPath := audited(t, &h, "maintain", container)
	for _, c := range []struct {
		at        time.Time
		due, next string
	}{
		{time.Date(2026, 11, 1, 0, 0, 0, 0, time.UTC), "ensemble-broker-env/dummy@1:30d", "2026-11-01T01:00:00Z"},
		{time.Date(2026, 11, 24, 0, 0, 0, 0, time.UTC), "ensemble-broker-env/dummy@1:7d", "2026-11-24T01:00:00Z"},
		{time.Date(2026, 11, 30, 0, 0, 0, 0, time.UTC), "ensemble-broker-env/dummy@1:1d", "2026-11-30T01:00:00Z"},
		{time.Date(2026, 12, 1, 0, 0, 0, 0, time.UTC), "ensemble-broker-env/dummy@1:expired", "2026-12-01T01:00:00Z"},
	} {
		now = c.at
		got, err := h.Maintain(context.Background(), container, path, nil, false)
		if err != nil || !slices.Equal(noticeKeys(got), []string{c.due}) || *got.NextCheck != c.next {
			t.Fatalf("%v: %+v %v", c.at, got, err)
		}
		got, err = h.Maintain(context.Background(), container, path, []string{c.due}, false)
		if err != nil || len(got.Notices) != 0 {
			t.Fatalf("ack %v: %+v %v", c.at, got, err)
		}
		if again, err := h.Maintain(context.Background(), container, path, nil, false); err != nil || len(again.Notices) != 0 {
			t.Fatalf("after ack %v: %+v %v", c.at, again, err)
		}
	}
	if err := h.Audit.Flush(nil); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(logPath)
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(strings.TrimSpace(string(data)), "\n")
	if len(lines) != 4 {
		t.Fatalf("%q", lines)
	}
	for i, th := range []string{"30d", "7d", "1d", "expired"} {
		if !strings.Contains(lines[i], `"event":"credential-notice"`) || !strings.Contains(lines[i], `"item":"ensemble-broker-env/dummy","notice":"`+th+`"`) ||
			!strings.Contains(lines[i], `"container":"`+container+`"`) {
			t.Errorf("%d: %s", i, lines[i])
		}
	}
	raw, _ := os.ReadFile(record)
	if !strings.Contains(string(raw), `"delivered":["ensemble-broker-env/dummy@1:30d","ensemble-broker-env/dummy@1:7d","ensemble-broker-env/dummy@1:1d","ensemble-broker-env/dummy@1:expired"]`) {
		t.Fatalf("%s", raw)
	}
}

func TestNoticesMostUrgentOnly(t *testing.T) {
	now := noticeNow
	h, path, _, _ := noticeHost(t, &now, func(s credential.StaticStore) {
		login(t, s, "ensemble-broker-env/ssh", credential.Registration{Expires: noticeNow.Add(5 * 24 * time.Hour)}, noticeNow)
	})
	got, err := h.Maintain(context.Background(), container, path, nil, false)
	if err != nil || !slices.Equal(noticeKeys(got), []string{"ensemble-broker-env/ssh@1:7d"}) {
		t.Fatalf("%+v %v", got, err)
	}
}

func TestNoticesIgnoresUndueKey(t *testing.T) {
	now := noticeNow
	h, path, _, record := noticeHost(t, &now, func(s credential.StaticStore) {
		login(t, s, "ensemble-broker-env/dummy", credential.Registration{Expires: noticeNow.Add(20 * 24 * time.Hour)}, noticeNow)
	})
	got, err := h.Maintain(context.Background(), container, path, []string{"ensemble-broker-env/dummy@1:7d", "ensemble-broker-env/ssh@1:30d", "ensemble-broker-env/dummy@9:30d"}, false)
	if err != nil || !slices.Equal(noticeKeys(got), []string{"ensemble-broker-env/dummy@1:30d"}) {
		t.Fatalf("%+v %v", got, err)
	}
	raw, _ := os.ReadFile(record)
	if !strings.Contains(string(raw), `"delivered":[]`) {
		t.Fatalf("%s", raw)
	}
}

func TestNoticesRotated(t *testing.T) {
	now := noticeNow
	h, path, store, _ := noticeHost(t, &now, func(s credential.StaticStore) {
		login(t, s, "ensemble-broker-env/dummy", credential.Registration{}, noticeNow)
	})
	if got, err := h.Maintain(context.Background(), container, path, nil, false); err != nil || len(got.Notices) != 0 {
		t.Fatalf("before: %+v %v", got, err)
	}
	login(t, store, "ensemble-broker-env/dummy", credential.Registration{}, noticeNow)
	got, err := h.Maintain(context.Background(), container, path, nil, false)
	want := "ensemble-broker-env/dummy was replaced (version 2); this session holds version 1 until it restarts"
	if err != nil || len(got.Notices) != 1 || got.Notices[0].Key != "ensemble-broker-env/dummy@2:rotated" || got.Notices[0].Text != want {
		t.Fatalf("%+v %v", got, err)
	}
	if _, err := h.Maintain(context.Background(), container, path, []string{"ensemble-broker-env/dummy@2:rotated"}, false); err != nil {
		t.Fatal(err)
	}
	if got, err := h.Maintain(context.Background(), container, path, nil, false); err != nil || len(got.Notices) != 0 {
		t.Fatalf("after ack: %+v %v", got, err)
	}
	// The next start injects version 2: no rotated notice.
	next, nextPath, _, _ := noticeHost(t, &now, func(s credential.StaticStore) {
		login(t, s, "ensemble-broker-env/dummy", credential.Registration{}, noticeNow)
		login(t, s, "ensemble-broker-env/dummy", credential.Registration{}, noticeNow)
	})
	if got, err := next.Maintain(context.Background(), container, nextPath, nil, false); err != nil || len(got.Notices) != 0 {
		t.Fatalf("next start: %+v %v", got, err)
	}
}

func TestNoticesWithoutRecord(t *testing.T) {
	now := noticeNow
	h, path, _, record := noticeHost(t, &now, nil)
	if err := os.Remove(record); err != nil {
		t.Fatal(err)
	}
	got, err := h.Maintain(context.Background(), container, path, nil, false)
	if err != nil || got.Notices == nil || len(got.Notices) != 0 || got.NextCheck != nil {
		t.Fatalf("%+v %v", got, err)
	}
	if encoded, _ := json.Marshal(got); string(encoded) != `{"container":"`+container+`","next_check":null,"deadline":null,"results":[],"notices":[]}` {
		t.Fatalf("%s", encoded)
	}
}

func TestNoticesCorruptRecord(t *testing.T) {
	now := noticeNow
	h, path, _, record := noticeHost(t, &now, nil)
	if err := os.WriteFile(record, []byte("SENTINEL-not-json"), 0o600); err != nil {
		t.Fatal(err)
	}
	got, err := h.Maintain(context.Background(), container, path, nil, false)
	if err != nil || len(got.Results) != 1 || got.Results[0].Credential != "notices" {
		t.Fatalf("%+v %v", got, err)
	}
	if e := got.Results[0].Error; e == nil || e.Code != protocol.InternalError || strings.Contains(e.Message, "SENTINEL") || !strings.Contains(e.Message, filepath.Base(record)) {
		t.Fatalf("%+v", e)
	}
}

// The value start injected, envValue-<item>, stays out of a notice check's output, audit line and records.
func TestNoticesLeaveOutEnvValue(t *testing.T) {
	now := noticeNow
	h, path, _, record := noticeHost(t, &now, func(s credential.StaticStore) {
		login(t, s, "ensemble-broker-env/dummy", credential.Registration{Expires: noticeNow.Add(3 * 24 * time.Hour)}, noticeNow)
	})
	_, logPath := audited(t, &h, "maintain", container)
	got, err := h.Maintain(context.Background(), container, path, []string{"ensemble-broker-env/dummy@1:7d"}, false)
	if err != nil {
		t.Fatal(err)
	}
	if err := h.Audit.Flush(nil); err != nil {
		t.Fatal(err)
	}
	encoded, _ := json.Marshal(got)
	logged, _ := os.ReadFile(logPath)
	raw, _ := os.ReadFile(record)
	store, _ := os.ReadFile(filepath.Join(h.Home, ".ensemble/docker/static-secrets.json"))
	for name, text := range map[string]string{"output": string(encoded), "audit": string(logged), "record": string(raw), "store": string(store)} {
		if strings.Contains(text, envValue) {
			t.Errorf("%s: %s", name, text)
		}
	}
}

func TestNoticesAuditOnlyRecordedDeliveries(t *testing.T) {
	if os.Getuid() == 0 {
		t.Skip("root writes anyway")
	}
	now := noticeNow
	h, path, _, record := noticeHost(t, &now, func(s credential.StaticStore) {
		login(t, s, "ensemble-broker-env/dummy", credential.Registration{Expires: noticeNow.Add(20 * 24 * time.Hour)}, noticeNow)
	})
	_, logPath := audited(t, &h, "maintain", container)
	if err := os.Chmod(filepath.Dir(record), 0o500); err != nil {
		t.Fatal(err)
	}
	defer os.Chmod(filepath.Dir(record), 0o700)
	got, err := h.Maintain(context.Background(), container, path, []string{"ensemble-broker-env/dummy@1:30d"}, false)
	if err != nil || len(got.Results) != 1 || got.Results[0].Error == nil || got.Results[0].Error.Code != protocol.InternalError {
		t.Fatalf("%+v %v", got, err)
	}
	if err := h.Audit.Flush(err); err != nil {
		t.Fatal(err)
	}
	if data, _ := os.ReadFile(logPath); strings.Contains(string(data), "credential-notice") {
		t.Fatalf("audited an unrecorded delivery: %s", data)
	}
}
