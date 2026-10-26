package launcher

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"sync"
	"testing"
	"time"

	"ensemble/broker/internal/credential"
	"ensemble/broker/internal/protocol"
)

const tokensGeneration = "0123456789abcdef0123456789abcdef"

// tokensHost is a Host with a real, empty state directory for worker_1.
func tokensHost(t *testing.T) (Host, Manifest, string) {
	t.Helper()
	h := mountHost()
	h.Home = t.TempDir()
	h.Schedule = StateSchedule{Home: h.Home, Safe: h.Safe}
	parent := filepath.Join(h.Home, ".ensemble/docker-state", "worker_1")
	if err := os.MkdirAll(parent, 0o700); err != nil {
		t.Fatal(err)
	}
	return h, Manifest{Name: "worker_1", Generation: tokensGeneration}, parent
}

func readRecord(t *testing.T, parent string) map[string]any {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(parent, tokensGeneration+".tokens.json"))
	if err != nil {
		t.Fatal(err)
	}
	var got map[string]any
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatal(err)
	}
	return got
}

func TestTokenRecordReplacesOnlyItsKind(t *testing.T) {
	h, m, parent := tokensHost(t)
	at := time.Date(2026, 10, 18, 12, 1, 0, 0, time.UTC)
	steps := []struct {
		kind    credential.Kind
		expires []string
		retry   *time.Time
	}{
		{credential.Codex, []string{"2026-10-18T12:00:00Z"}, nil},
		{credential.GitHub, []string{"2026-10-18T13:00:00Z", "2026-10-18T12:30:00Z"}, nil},
		{credential.Codex, nil, &at},
	}
	for _, s := range steps {
		if err := h.Schedule.Record(m, s.kind, s.expires, s.retry); err != nil {
			t.Fatal(err)
		}
	}
	want := map[string]any{
		"expires": map[string]any{"codex": []any{"2026-10-18T12:00:00Z"}, "github": []any{"2026-10-18T13:00:00Z", "2026-10-18T12:30:00Z"}},
		"retry":   map[string]any{"codex": "2026-10-18T12:01:00Z"},
	}
	if got := readRecord(t, parent); !reflect.DeepEqual(got, want) {
		t.Fatalf("%v", got)
	}
	if err := h.Schedule.Record(m, credential.Codex, []string{"2026-10-18T14:00:00Z"}, nil); err != nil {
		t.Fatal(err)
	}
	got := h.Schedule.Read(m)
	if !slices.Equal(got.Expires[credential.Codex], []string{"2026-10-18T14:00:00Z"}) || len(got.Retry) != 0 || len(got.Expires[credential.GitHub]) != 2 {
		t.Fatalf("%+v", got)
	}
	if info, err := os.Stat(filepath.Join(parent, tokensGeneration+".tokens.json")); err != nil || info.Mode().Perm() != 0o600 {
		t.Fatalf("%v %v", info, err)
	}
}

func TestTokenRecordConcurrentKindsBothPersist(t *testing.T) {
	h, m, _ := tokensHost(t)
	var wg sync.WaitGroup
	errs := make(chan error, 200)
	for _, kind := range []credential.Kind{credential.Codex, credential.GitHub} {
		wg.Add(1)
		go func(kind credential.Kind) {
			defer wg.Done()
			for i := range 100 {
				at := time.Date(2026, 10, 18, 12, 0, i%60, 0, time.UTC).Format(time.RFC3339)
				errs <- h.Schedule.Record(m, kind, []string{at}, nil)
			}
		}(kind)
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatal(err)
		}
	}
	got := h.Schedule.Read(m)
	if len(got.Expires[credential.Codex]) != 1 || len(got.Expires[credential.GitHub]) != 1 {
		t.Fatalf("a kind was lost: %+v", got)
	}
}

func TestTokenRecordRefusesLinkedLockAndUnsafeDir(t *testing.T) {
	h, m, parent := tokensHost(t)
	outside := filepath.Join(t.TempDir(), "elsewhere")
	if err := os.Symlink(outside, filepath.Join(parent, tokensGeneration+".tokens.lock")); err != nil {
		t.Fatal(err)
	}
	err := h.Schedule.Record(m, credential.Codex, []string{"2026-10-18T12:00:00Z"}, nil)
	if e := protoErr(t, err); e.Code != protocol.InternalError || e.Message != "Recording "+tokensGeneration+".tokens.json failed." {
		t.Fatalf("%+v", e)
	}
	if _, err := os.Lstat(outside); !os.IsNotExist(err) {
		t.Fatalf("followed the link: %v", err)
	}
	h, m, _ = tokensHost(t)
	h.Schedule = StateSchedule{Home: h.Home, Safe: func(string) error { return errors.New("symlink component") }}
	if err := h.Schedule.Record(m, credential.Codex, []string{"2026-10-18T12:00:00Z"}, nil); err == nil {
		t.Fatal("wrote through an unsafe state directory")
	}
	if got := h.Schedule.Read(m); len(got.Expires) != 0 {
		t.Fatalf("%+v", got)
	}
}

func TestReadTokensWithoutOrWithCorruptRecordIsEmpty(t *testing.T) {
	h, m, parent := tokensHost(t)
	if got := h.Schedule.Read(m); len(got.Expires) != 0 || len(got.Retry) != 0 {
		t.Fatalf("%+v", got)
	}
	if err := os.WriteFile(filepath.Join(parent, tokensGeneration+".tokens.json"), []byte("{"), 0o600); err != nil {
		t.Fatal(err)
	}
	if got := h.Schedule.Read(m); len(got.Expires) != 0 || got.Retry == nil {
		t.Fatalf("%+v", got)
	}
}

// tokenStartHost starts a Codex worker_1 with GitHub tokens in a real state directory.
func tokenStartHost(t *testing.T) (Host, string, string, *fakeTokens, *fakeCodexCreds, *fakeDocker) {
	t.Helper()
	h, d := startHost(&world{labels: goodLabels(), versions: []string{"claude 2", "codex-cli 0.163.0"}}, &fakeCreds{})
	h.Home = t.TempDir()
	h.Schedule = StateSchedule{Home: h.Home, Safe: h.Safe}
	h.Versions = &fakeVersions{}
	tokens, creds := &fakeTokens{}, &fakeCodexCreds{}
	h.GitHub, h.Codex = tokens, creds
	parent := filepath.Join(h.Home, ".ensemble/docker-state", "worker_1")
	if err := os.MkdirAll(parent, 0o700); err != nil {
		t.Fatal(err)
	}
	path := codexManifest(t, func(f map[string]any) {
		f["codex_dir"] = filepath.Join(parent, "codex")
		f["github"] = map[string]any{"app": "ensemble-bot", "permissions": fixtureSet}
	})
	return h, path, parent, tokens, creds, d
}

func TestStartWritesTokenRecord(t *testing.T) {
	h, path, parent, _, _, _ := tokenStartHost(t)
	if _, err := h.Start(context.Background(), path); err != nil {
		t.Fatal(err)
	}
	got := readRecord(t, parent)
	want := map[string]any{
		"expires": map[string]any{"codex": []any{"2026-10-18T12:00:00Z"}, "github": []any{expiry[0].ExpiresAt}},
		"retry":   map[string]any{},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("%v", got)
	}
}

func TestStartTokenRecordFailureRollsBack(t *testing.T) {
	h, path, parent, tokens, creds, d := tokenStartHost(t)
	if err := os.Symlink(filepath.Join(t.TempDir(), "elsewhere"), filepath.Join(parent, tokensGeneration+".tokens.lock")); err != nil {
		t.Fatal(err)
	}
	_, err := h.Start(context.Background(), path)
	e := protoErr(t, err)
	if e.Code != protocol.StartFailed || e.Message != "Recording "+tokensGeneration+".tokens.json failed." || e.Cleanup != protocol.Removed {
		t.Fatalf("%+v", e)
	}
	if !hasCall(d, "rm") || !slices.Contains(tokens.calls, "revoke "+container) || !slices.Equal(creds.released, []string{container}) {
		t.Fatalf("no full rollback: %q %q %q", d.calls, tokens.calls, creds.released)
	}
	if _, err := os.Stat(filepath.Join(parent, tokensGeneration+".tokens.json")); !os.IsNotExist(err) {
		t.Fatalf("record left behind: %v", err)
	}
}
