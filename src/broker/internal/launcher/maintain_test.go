package launcher

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"ensemble/broker/internal/credential"
	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/protocol"
)

var maintainNow = time.Date(2026, 10, 17, 12, 0, 0, 0, time.UTC)

const codexReason = "Client authentication expired; run /usr/local/libexec/ensemble/ensemble-broker login codex on the host, then resume."

// maintainHost is a running Codex worker_1 container (with GitHub tokens when github) whose token record, kind → its
// expiries, is seeded unless nil; the clock is maintainNow.
func maintainHost(t *testing.T, github bool, seed map[credential.Kind][]string) (Host, string, *fakeTokens, *fakeCodexCreds) {
	t.Helper()
	h, manifest, _, tokens, creds := refreshCodexHost(t, github)
	h.Schedule = StateSchedule{Home: h.Home, Safe: h.Safe}
	h.Now = func() time.Time { return maintainNow }
	for kind, expires := range seed {
		if err := h.Schedule.Record(tokensManifest, kind, expires, nil); err != nil {
			t.Fatal(err)
		}
	}
	return h, manifest, tokens, creds
}

var tokensManifest = Manifest{Name: "worker_1", Generation: tokensGeneration}

func soon(d time.Duration) string { return maintainNow.Add(d).Format(time.RFC3339) }

func results(r MaintainResult) map[string]ProviderResult {
	out := map[string]ProviderResult{}
	for _, p := range r.Results {
		out[p.Credential] = p
	}
	return out
}

func TestMaintainCodexFailureKeepsGitHubSuccess(t *testing.T) {
	h, manifest, tokens, creds := maintainHost(t, true, map[credential.Kind][]string{
		credential.Codex: {soon(5 * time.Minute)}, credential.GitHub: {soon(5 * time.Minute)},
	})
	creds.err = protocol.New(protocol.RefreshFailed, "Renewal unavailable.")
	got, err := h.Maintain(context.Background(), container, manifest, false)
	if err != nil {
		t.Fatal(err)
	}
	r := results(got)
	if c := r["codex"]; c.Error == nil || c.Error.Code != protocol.RefreshFailed || c.Error.Message != "Renewal unavailable." || c.ExpiresAt != nil {
		t.Fatalf("codex %+v", c)
	}
	if g := r["github"]; g.Error != nil || g.ExpiresAt == nil || *g.ExpiresAt != expiry[0].ExpiresAt {
		t.Fatalf("github %+v", g)
	}
	record := h.Schedule.Read(tokensManifest)
	if !slices.Equal(record.Expires[credential.GitHub], []string{expiry[0].ExpiresAt}) ||
		!slices.Equal(record.Expires[credential.Codex], []string{soon(5 * time.Minute)}) || record.Retry[credential.Codex] != soon(time.Minute) {
		t.Fatalf("record %+v", record)
	}
	if got.Deadline == nil || *got.Deadline != (Deadline{At: soon(5 * time.Minute), Action: "close", Reason: codexReason}) {
		t.Fatalf("deadline %+v", got.Deadline)
	}
	if got.NextCheck == nil || *got.NextCheck != soon(time.Minute) || !slices.Contains(tokens.calls, "refresh ensemble-broker-bot "+container) {
		t.Fatalf("%+v %q", got, tokens.calls)
	}
}

func TestMaintainGitHubFailureKeepsCodexSuccess(t *testing.T) {
	h, manifest, tokens, creds := maintainHost(t, true, nil)
	tokens.refresh = errors.New("opaque")
	got, err := h.Maintain(context.Background(), container, manifest, false)
	if err != nil {
		t.Fatal(err)
	}
	r := results(got)
	if g := r["github"]; g.Error == nil || g.Error.Code != protocol.RefreshFailed || g.Error.Message != "Writing a GitHub token into "+container+" failed." {
		t.Fatalf("github %+v", g)
	}
	if c := r["codex"]; c.Error != nil || c.ExpiresAt == nil || *c.ExpiresAt != "2026-10-18T12:00:00Z" || creds.injected != 1 {
		t.Fatalf("codex %+v", c)
	}
	record := h.Schedule.Read(tokensManifest)
	if !slices.Equal(record.Expires[credential.Codex], []string{"2026-10-18T12:00:00Z"}) || len(record.Expires[credential.GitHub]) != 0 ||
		record.Retry[credential.GitHub] != soon(time.Minute) {
		t.Fatalf("record %+v", record)
	}
	if got.Deadline == nil || got.Deadline.At != "2026-10-18T12:00:00Z" || got.NextCheck == nil || *got.NextCheck != soon(time.Minute) {
		t.Fatalf("%+v", got)
	}
	// The immediate call that acknowledges a notice must not try GitHub again before its retry time.
	tokens.calls = nil
	again, err := h.Maintain(context.Background(), container, manifest, false)
	if err != nil || len(again.Results) != 0 || slices.Contains(tokens.calls, "refresh ensemble-broker-bot "+container) {
		t.Fatalf("%+v %v %q", again, err, tokens.calls)
	}
}

func TestMaintainRenewsOnlyDueProviders(t *testing.T) {
	h, manifest, tokens, creds := maintainHost(t, true, map[credential.Kind][]string{
		credential.Codex: {"2026-10-18T12:00:00Z"}, credential.GitHub: {soon(9 * time.Minute)},
	})
	got, err := h.Maintain(context.Background(), container, manifest, false)
	if err != nil {
		t.Fatal(err)
	}
	if creds.injected != 0 || len(got.Results) != 1 || got.Results[0].Credential != "github" || !slices.Contains(tokens.calls, "refresh ensemble-broker-bot "+container) {
		t.Fatalf("%+v %d", got, creds.injected)
	}
	// GitHub's new expiry 13:00 is due at 12:50, before Codex's at 11:50 the next day.
	if got.NextCheck == nil || *got.NextCheck != "2026-10-17T12:50:00Z" {
		t.Fatalf("%v", got.NextCheck)
	}
}

func TestMaintainNextCheck(t *testing.T) {
	for _, c := range []struct {
		name    string
		seed    map[credential.Kind][]string
		retry   *time.Time
		want    *string
		renewed int
	}{
		{name: "not due", seed: map[credential.Kind][]string{credential.Codex: {soon(2 * time.Hour)}}, want: ptr(soon(110 * time.Minute))},
		{name: "past due renews", seed: map[credential.Kind][]string{credential.Codex: {soon(-time.Hour)}}, want: ptr("2026-10-18T11:50:00Z"), renewed: 1},
		{name: "retry pending", seed: map[credential.Kind][]string{credential.Codex: {soon(time.Minute)}}, retry: ptr(maintainNow.Add(30 * time.Second)), want: ptr(soon(30 * time.Second))},
	} {
		t.Run(c.name, func(t *testing.T) {
			h, manifest, _, creds := maintainHost(t, false, c.seed)
			if c.retry != nil {
				if err := h.Schedule.Record(tokensManifest, credential.Codex, nil, c.retry); err != nil {
					t.Fatal(err)
				}
			}
			got, err := h.Maintain(context.Background(), container, manifest, false)
			if err != nil || creds.injected != c.renewed || (got.NextCheck == nil) != (c.want == nil) || got.NextCheck != nil && *got.NextCheck != *c.want {
				t.Fatalf("%+v %v injected %d", got, err, creds.injected)
			}
		})
	}
}

func ptr[T any](v T) *T { return &v }

func TestMaintainWithoutTokenProvidersNeverChecksAgain(t *testing.T) {
	h, _ := startHost(&world{labels: goodLabels()}, &fakeCreds{})
	got, err := h.Maintain(context.Background(), container, validManifest(t), false)
	if err != nil || got.NextCheck != nil || got.Deadline != nil || len(got.Results) != 0 {
		t.Fatalf("%+v %v", got, err)
	}
	encoded, _ := json.Marshal(got)
	if string(encoded) != `{"container":"`+container+`","next_check":null,"deadline":null,"results":[]}` {
		t.Fatalf("%s", encoded)
	}
}

func TestMaintainRenewFlag(t *testing.T) {
	far := map[credential.Kind][]string{credential.Codex: {"2026-10-20T12:00:00Z"}, credential.GitHub: {"2026-10-20T12:00:00Z"}}
	h, manifest, tokens, creds := maintainHost(t, true, far)
	got, err := h.Maintain(context.Background(), container, manifest, true)
	if err != nil || len(got.Results) != 2 || creds.injected != 1 || !slices.Contains(tokens.calls, "refresh ensemble-broker-bot "+container) {
		t.Fatalf("%+v %v", got, err)
	}
}

func TestMaintainWithoutRecordCodexFailureHasNoDeadline(t *testing.T) {
	h, manifest, _, creds := maintainHost(t, false, nil)
	creds.err = protocol.New(protocol.RefreshFailed, "Renewal unavailable.")
	got, err := h.Maintain(context.Background(), container, manifest, false)
	if err != nil || got.Deadline != nil || len(got.Results) != 1 || got.Results[0].Error == nil || got.NextCheck == nil || *got.NextCheck != soon(time.Minute) {
		t.Fatalf("%+v %v", got, err)
	}
}

func TestDeadlineUsesEarliestHeldProvider(t *testing.T) {
	for _, first := range []credential.Kind{credential.Linear, credential.Codex} {
		record := TokenRecord{Expires: map[credential.Kind][]string{
			credential.Linear: {soon(time.Hour)}, credential.Codex: {soon(time.Hour)},
		}}
		record.Expires[first] = []string{soon(-time.Minute)}
		deadline := deadlineOf(record, []credential.Kind{credential.Codex, credential.Linear})
		if deadline == nil || deadline.At != soon(-time.Minute) {
			t.Fatalf("%s expired first: %+v", first, deadline)
		}
		want := "Linear"
		if first == credential.Codex {
			want = "Client"
		}
		if !strings.HasPrefix(deadline.Reason, want) {
			t.Fatal("deadline reason belongs to another provider")
		}
	}
}

func TestMaintainRedactsProviderErrors(t *testing.T) {
	const sentinel = "SENTINEL-m41nt41n"
	h, manifest, tokens, creds := maintainHost(t, true, nil)
	creds.err = &docker.CommandError{Exit: 1, Stderr: "writer said " + sentinel}
	tokens.refresh = &protocol.Error{Code: protocol.GitHubFailed, Message: "GitHub failed.\nforged line", Cause: "status 500\x07"}
	_, logPath := audited(t, &h, "maintain", container)
	got, err := h.Maintain(context.Background(), container, manifest, false)
	if err != nil {
		t.Fatal(err)
	}
	if err := h.Audit.Flush(nil); err != nil {
		t.Fatal(err)
	}
	encoded, _ := json.Marshal(got)
	logged, _ := os.ReadFile(logPath)
	for name, text := range map[string]string{"output": string(encoded), "audit": string(logged)} {
		if strings.Contains(text, sentinel) || strings.Contains(text, "ghs_token") {
			t.Errorf("%s: %s", name, text)
		}
	}
	g := results(got)["github"].Error
	if g == nil || g.Message != `GitHub failed.\x0aforged line` || g.Cause != `status 500\x07` {
		t.Fatalf("%+v", g)
	}
	lines := readLines(t, logPath)
	if len(lines) != 1 || lines[0].Event != "refresh" || lines[0].Command != "maintain" || lines[0].Error == nil || *lines[0].Error != string(protocol.RefreshFailed) {
		t.Fatalf("%+v", lines)
	}
}

func TestMaintainWritesNoRefreshLineWhenNothingIsDue(t *testing.T) {
	h, manifest, _, _ := maintainHost(t, false, map[credential.Kind][]string{credential.Codex: {"2026-10-18T12:00:00Z"}})
	rec, logPath := audited(t, &h, "maintain", container)
	if _, err := h.Maintain(context.Background(), container, manifest, false); err != nil {
		t.Fatal(err)
	}
	if err := rec.Flush(nil); err != nil {
		t.Fatal(err)
	}
	if data, err := os.ReadFile(logPath); err == nil && len(data) != 0 {
		t.Fatalf("%s", data)
	}
}

// A manifest that is unreadable, not the container's or not safely mounted fails Maintain before any provider runs.
func TestMaintainRejects(t *testing.T) {
	for name, want := range map[string]protocol.Code{
		"other container": protocol.ManifestRejected, "missing": protocol.ManifestRejected,
		"directory": protocol.MountRejected, "symlink": protocol.MountRejected,
	} {
		t.Run(name, func(t *testing.T) {
			h, manifest, tokens, creds := maintainHost(t, true, nil)
			c := container
			switch name {
			case "other container":
				c = "ensemble-other-" + tokensGeneration
			case "missing":
				manifest = filepath.Join(t.TempDir(), "missing.json")
			case "directory":
				h.Home = "/arbitrary"
			case "symlink":
				h.Safe = func(string) error { return errors.New("link") }
			}
			_, err := h.Maintain(context.Background(), c, manifest, false)
			if code(t, err) != want || creds.injected != 0 || len(tokens.calls) != 0 {
				t.Fatalf("%v %d %q", err, creds.injected, tokens.calls)
			}
		})
	}
}
