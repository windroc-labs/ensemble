package launcher

import (
	"context"
	"encoding/json"
	"errors"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"ensemble/broker/internal/credential"
)

type fakeLinear struct {
	calls    []string
	prepared [][]byte
	fail     string
	onRevoke func() error
}

func (f *fakeLinear) Ready(credential.LinearApp) error {
	f.calls = append(f.calls, "ready")
	if f.fail == "ready" {
		return errors.New("private-token")
	}
	return nil
}
func (f *fakeLinear) Issue(_ context.Context, r credential.LinearRequest, _ string) ([]credential.Command, func(), string, error) {
	f.calls = append(f.calls, "issue")
	data, _ := json.Marshal(r.Board)
	token, _ := credential.Prepare(credential.File{Dir: credential.LinearDir, Name: "token.json"}, []byte("private-token"))
	board, _ := credential.Prepare(credential.File{Dir: credential.LinearDir, Name: "board.json"}, data)
	f.prepared = append(f.prepared, token.Stdin, board.Stdin)
	return []credential.Command{token, board}, func() { f.calls = append(f.calls, "rollback") }, "2026-11-18T12:00:00Z", nil
}
func (f *fakeLinear) Refresh(context.Context, credential.LinearRequest, string, func([]string, []byte) error) (string, error) {
	f.calls = append(f.calls, "refresh")
	if f.fail == "refresh" {
		return "", errors.New("private-token")
	}
	return "2026-11-18T12:00:00Z", nil
}
func (f *fakeLinear) Revoke(context.Context, string) error {
	f.calls = append(f.calls, "revoke")
	if f.onRevoke != nil {
		return f.onRevoke()
	}
	return nil
}

func linearManifest(t *testing.T) string {
	return editedManifest(t, func(m map[string]any) { m["credentials"] = string(fixtureBytes(t, "credential-spec-linear")) })
}

func TestLinearStartAndRollback(t *testing.T) {
	for _, fail := range []string{"", "ready", "delivery", "schedule"} {
		t.Run(fail, func(t *testing.T) {
			w := &world{labels: goodLabels()}
			if fail == "delivery" {
				w.batchReply = ptr(`{"completed":2}`)
			}
			h, d := startHost(w, &fakeCreds{})
			f := &fakeLinear{fail: fail}
			h.Linear = f
			if fail == "schedule" {
				h.Schedule = StateSchedule{Home: t.TempDir(), Safe: func(string) error { return nil }}
			}
			result, err := h.Start(context.Background(), linearManifest(t))
			if (err == nil) != (fail == "") {
				t.Fatalf("%v", err)
			}
			if fail == "ready" {
				if hasCall(d, "run") || strings.Contains(err.Error(), "private-token") {
					t.Fatal("unsafe preflight")
				}
				return
			}
			commands := startCommands(t, d)
			if len(commands) != 3 || !strings.Contains(strings.Join(commands[1].Argv, " "), credential.LinearDir) || strings.Contains(string(commands[2].Stdin), "private-token") {
				t.Fatal("wrong batch")
			}
			if (fail != "") != slices.Contains(f.calls, "rollback") {
				t.Fatalf("rollback %v", f.calls)
			}
			if fail == "" {
				if result.Container == "" {
					t.Fatal("no container")
				}
				if _, err := h.Close(context.Background(), result.Container); err != nil {
					t.Fatal(err)
				}
				if !slices.Contains(f.calls, "revoke") {
					t.Fatal("close missed linear")
				}
			}
			for _, b := range f.prepared {
				if slices.ContainsFunc(b, func(c byte) bool { return c != 0 }) {
					t.Fatal("buffer not erased")
				}
			}
		})
	}
	h, _ := startHost(&world{labels: goodLabels()}, &fakeCreds{})
	f := &fakeLinear{}
	h.Linear = f
	if _, err := h.Start(context.Background(), editedManifest(t, nil)); err != nil {
		t.Fatal(err)
	}
	if len(f.calls) > 0 {
		t.Fatal("undeclared linear touched")
	}
}

func TestLinearMaintenance(t *testing.T) {
	for _, fail := range []bool{false, true} {
		h, _ := startHost(&world{labels: goodLabels()}, &fakeCreds{})
		h.Now = func() time.Time { return maintainNow }
		f := &fakeLinear{}
		h.Linear = f
		if fail {
			f.fail = "refresh"
		}
		result, err := h.Maintain(context.Background(), container, linearManifest(t), true)
		if err != nil || !slices.Contains(f.calls, "refresh") || len(result.Results) != 1 {
			t.Fatalf("%+v %v calls %v", result, err, f.calls)
		}
		if (result.Results[0].Error != nil) != fail || strings.Contains(resultErrorText(result), "private-token") {
			t.Fatal("unsafe maintenance result")
		}
	}
}

func resultErrorText(r MaintainResult) string { data, _ := json.Marshal(r); return string(data) }

func TestLinearCloseIncludesStartupDelivery(t *testing.T) {
	held := false
	f := &fakeLinear{onRevoke: func() error { held = false; return nil }}
	d := &fakeDocker{respond: func(args []string, _ []byte) ([]byte, error) {
		if args[0] == "rm" {
			// Initial startup delivers while close is about to remove its container.
			held = true
			return []byte(container + "\n"), nil
		}
		return nil, nil
	}}
	h := Host{Docker: d, GitHub: &fakeTokens{}, Linear: f}
	if _, err := h.Close(context.Background(), container); err != nil {
		t.Fatal(err)
	}
	if held {
		t.Fatal("close missed credentials delivered during startup")
	}
}

func TestLinearStartRecordsExpiry(t *testing.T) {
	h, _, parent, _, _, _ := tokenStartHost(t)
	h.Linear = &fakeLinear{}
	path := codexManifest(t, func(f map[string]any) {
		f["codex_dir"] = filepath.Join(parent, "codex")
		f["environment_credentials"] = githubSpec(t, map[string]any{"app": "ensemble-broker-bot", "permissions": fixtureSet})
		f["credentials"] = string(fixtureBytes(t, "credential-spec-linear"))
	})
	if _, err := h.Start(context.Background(), path); err != nil {
		t.Fatal(err)
	}
	got := h.Schedule.Read(Manifest{Name: "worker_1", Generation: tokensGeneration})
	if !slices.Equal(got.Expires[credential.Linear], []string{"2026-11-18T12:00:00Z"}) ||
		!slices.Equal(got.Expires[credential.Codex], []string{"2026-10-18T12:00:00Z"}) ||
		!slices.Equal(got.Expires[credential.GitHub], []string{expiry[0].ExpiresAt}) {
		t.Fatalf("wrong delivered expiries: %+v", got.Expires)
	}
}
