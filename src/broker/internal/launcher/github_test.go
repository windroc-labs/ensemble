package launcher

import (
	"context"
	"encoding/json"
	"errors"
	"maps"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/fixtures"
	"ensemble/broker/internal/protocol"
)

var expiry = []protocol.Expiry{{Account: "acme", ExpiresAt: "2026-10-17T13:00:00Z"}}

// fakeTokens records the launcher's Tokens calls; each err field fails its method.
type fakeTokens struct {
	calls                                      []string
	ready, user, issue, refresh, revoke, retry error
	pending                                    []protocol.Pending
	onRevoke                                   func()
	permissions                                []map[string]string // each Issue's and Refresh's set
}

// fixtureSet is manifest-github-app.json's permissions.
var fixtureSet = map[string]string{"actions": "read", "checks": "read", "contents": "write", "pull_requests": "write", "statuses": "read", "workflows": "write"}

const botID int64 = 343764394

func (f *fakeTokens) Ready(app string) error { f.calls = append(f.calls, "ready "+app); return f.ready }
func (f *fakeTokens) BotUserID(app string) (int64, error) {
	f.calls = append(f.calls, "user "+app)
	if f.user != nil {
		return 0, f.user
	}
	return botID, nil
}
func (f *fakeTokens) Issue(_ context.Context, app string, permissions map[string]string, c string, exec func([]string, []byte) error) ([]protocol.Expiry, error) {
	f.calls, f.permissions = append(f.calls, "issue "+app+" "+c), append(f.permissions, permissions)
	if f.issue != nil {
		return nil, f.issue
	}
	return expiry, exec([]string{"write"}, []byte("ghs_token"))
}
func (f *fakeTokens) Refresh(_ context.Context, app string, permissions map[string]string, c string, exec func([]string, []byte) error) ([]protocol.Expiry, error) {
	f.calls, f.permissions = append(f.calls, "refresh "+app+" "+c), append(f.permissions, permissions)
	if f.refresh != nil {
		return nil, f.refresh
	}
	return expiry, exec([]string{"write"}, []byte("ghs_token"))
}
func (f *fakeTokens) Revoke(_ context.Context, c string) error {
	f.calls = append(f.calls, "revoke "+c)
	if f.onRevoke != nil {
		f.onRevoke()
	}
	return f.revoke
}
func (f *fakeTokens) Retry(context.Context) ([]protocol.Pending, error) {
	f.calls = append(f.calls, "retry")
	return f.pending, f.retry
}

func (f *fakeTokens) Pending() ([]protocol.Pending, error) {
	f.calls = append(f.calls, "pending")
	return f.pending, f.retry
}

func githubManifest(t *testing.T) string {
	data, err := os.ReadFile(filepath.Join(fixtures.Dir(), "manifest-github-app.json"))
	if err != nil {
		t.Fatal(err)
	}
	return writeManifest(t, data)
}

func githubHost(w *world, tokens *fakeTokens) (Host, *fakeDocker) {
	h, d := startHost(w, &fakeCreds{})
	h.GitHub = tokens
	return h, d
}

func TestStartGitHub(t *testing.T) {
	tokens := &fakeTokens{}
	h, d := githubHost(&world{labels: goodLabels()}, tokens)
	got, err := h.Start(context.Background(), githubManifest(t))
	if err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(got.GitHub, expiry) || !slices.Equal(tokens.calls, []string{"ready ensemble-bot", "user ensemble-bot", "retry", "issue ensemble-bot " + container}) {
		t.Fatalf("%v %q", got.GitHub, tokens.calls)
	}
	if len(tokens.permissions) != 1 || !maps.Equal(tokens.permissions[0], fixtureSet) {
		t.Fatalf("issued with %v, want the manifest's set", tokens.permissions)
	}
	if got.GitHubUserID == nil || *got.GitHubUserID != botID {
		t.Fatalf("user ID %v", got.GitHubUserID)
	}
	if out, _ := json.Marshal(got); !strings.Contains(string(out), `"github_user_id":343764394`) {
		t.Fatalf("%s", out)
	}
	last := d.calls[len(d.calls)-1]
	if !slices.Equal(last, []string{"exec", "--interactive", "--user", "agent", container, "write"}) || string(d.stdin[len(d.stdin)-1]) != "ghs_token" {
		t.Fatalf("write %q", last)
	}
	inject := slices.IndexFunc(d.calls, func(c []string) bool { return slices.Contains(c, "inject") })
	if inject < 0 || inject > len(d.calls)-2 {
		t.Fatal("tokens written before the Claude token")
	}
}

func TestStartGitHubNull(t *testing.T) {
	tokens := &fakeTokens{}
	h, _ := githubHost(&world{labels: goodLabels()}, tokens)
	got, err := h.Start(context.Background(), validManifest(t))
	if err != nil || got.GitHub == nil || len(got.GitHub) != 0 || got.GitHubUserID != nil || !slices.Equal(tokens.calls, []string{"retry"}) {
		t.Fatalf("%v %v %v %q", got.GitHub, got.GitHubUserID, err, tokens.calls)
	}
	if out, _ := json.Marshal(got); !strings.Contains(string(out), `"github_user_id":null`) {
		t.Fatalf("%s", out)
	}
}

func TestStartGitHubRejectedBeforeContainer(t *testing.T) {
	for _, code := range []protocol.Code{protocol.ManifestRejected, protocol.LoginRequired, protocol.KeychainFailed} {
		for _, tokens := range []*fakeTokens{{ready: protocol.New(code, "no")}, {user: protocol.New(code, "no")}} {
			h, d := githubHost(&world{labels: goodLabels()}, tokens)
			_, err := h.Start(context.Background(), githubManifest(t))
			if e := protoErr(t, err); e.Code != code || e.Cleanup != protocol.NotNeeded || hasCall(d, "run") || hasCall(d, "volume create") {
				t.Fatalf("%s %q: %+v %q", code, tokens.calls, e, d.verbs())
			}
		}
	}
}

func TestStartGitHubFailureRollsBack(t *testing.T) {
	for name, tc := range map[string]struct {
		err  error
		code protocol.Code
	}{
		"github":      {protocol.New(protocol.GitHubFailed, "GitHub request failed: GitHub answered HTTP 500."), protocol.GitHubFailed},
		"unreachable": {errors.Join(errors.New("x"), docker.ErrUnreachable), protocol.DockerUnreachable},
		"opaque":      {errors.New("writing a GitHub token into the container failed"), protocol.StartFailed},
	} {
		t.Run(name, func(t *testing.T) {
			h, d := githubHost(&world{labels: goodLabels()}, &fakeTokens{issue: tc.err})
			_, err := h.Start(context.Background(), githubManifest(t))
			e := protoErr(t, err)
			if e.Code != tc.code || e.Cleanup != protocol.Removed || !hasCall(d, "rm") {
				t.Fatalf("%+v", e)
			}
		})
	}
}

func TestStartRetryStderr(t *testing.T) {
	var progress strings.Builder
	h, _ := githubHost(&world{labels: goodLabels()}, &fakeTokens{retry: protocol.New(protocol.KeychainFailed, "unreadable")})
	h.Progress = &progress
	if _, err := h.Start(context.Background(), validManifest(t)); err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(progress.String(), "ensemble-broker: start revocations skipped\nensemble-broker: start preflight ") {
		t.Fatalf("%q", progress.String())
	}
	progress.Reset()
	h, _ = githubHost(&world{labels: goodLabels(), runErr: errors.New("boom")}, &fakeTokens{retry: errors.New("x")})
	h.Progress = &progress
	if _, err := h.Start(context.Background(), validManifest(t)); err == nil || progress.Len() != 0 {
		t.Fatalf("%v %q", err, progress.String())
	}
}

func TestCloseRevokesFirst(t *testing.T) {
	var order []string
	d := &fakeDocker{respond: func(args []string) ([]byte, error) {
		order = append(order, args[0])
		return []byte(container + "\n"), nil
	}}
	tokens := &fakeTokens{onRevoke: func() { order = append(order, "revoke") }}
	got, err := Host{Docker: d, GitHub: tokens}.Close(context.Background(), container)
	if err != nil || !got.Removed || !slices.Equal(order, []string{"revoke", "rm"}) {
		t.Fatalf("%+v %v %v", got, err, order)
	}
}

func TestCloseKeychainFails(t *testing.T) {
	d := &fakeDocker{respond: func([]string) ([]byte, error) { return []byte(container + "\n"), nil }}
	tokens := &fakeTokens{revoke: protocol.New(protocol.KeychainFailed, "The GitHub tokens could not be revoked; they expire within an hour.")}
	_, err := Host{Docker: d, GitHub: tokens}.Close(context.Background(), container)
	if protoErr(t, err).Code != protocol.KeychainFailed || !hasCall(d, "rm") {
		t.Fatalf("%v %q", err, d.calls)
	}
}

func TestRefresh(t *testing.T) {
	tokens := &fakeTokens{}
	d := &fakeDocker{}
	h := mountHost()
	h.Docker, h.GitHub = d, tokens
	got, err := h.Refresh(context.Background(), container, githubManifest(t))
	if err != nil || got.Container != container || !slices.Equal(got.GitHub, expiry) {
		t.Fatalf("%+v %v", got, err)
	}
	if !slices.Equal(d.calls[0], []string{"exec", "--interactive", "--user", "agent", container, "write"}) {
		t.Fatalf("%q", d.calls)
	}
	if slices.ContainsFunc(tokens.calls, func(c string) bool { return strings.HasPrefix(c, "user ") }) {
		t.Fatalf("refresh read the bot user ID: %q", tokens.calls)
	}
	if len(tokens.permissions) != 1 || !maps.Equal(tokens.permissions[0], fixtureSet) {
		t.Fatalf("refreshed with %v, want the manifest's set", tokens.permissions)
	}
	other := "ensemble-worker_1-ffffffffffffffffffffffffffffffff"
	for name, tc := range map[string]struct {
		container, manifest string
		tokens              *fakeTokens
		code                protocol.Code
	}{
		"other container": {other, githubManifest(t), &fakeTokens{}, protocol.ManifestRejected},
		"github null":     {container, validManifest(t), &fakeTokens{}, protocol.ManifestRejected},
		"missing":         {container, "/nonexistent/m.json", &fakeTokens{}, protocol.ManifestRejected},
		"no key":          {container, githubManifest(t), &fakeTokens{ready: protocol.New(protocol.LoginRequired, "x")}, protocol.LoginRequired},
		"no record":       {container, githubManifest(t), &fakeTokens{refresh: protocol.New(protocol.RefreshFailed, "x")}, protocol.RefreshFailed},
		"write":           {container, githubManifest(t), &fakeTokens{refresh: errors.New("opaque")}, protocol.RefreshFailed},
		"unreachable":     {container, githubManifest(t), &fakeTokens{refresh: docker.ErrUnreachable}, protocol.DockerUnreachable},
	} {
		host := mountHost()
		host.Docker, host.GitHub = &fakeDocker{}, tc.tokens
		_, err := host.Refresh(context.Background(), tc.container, tc.manifest)
		if protoErr(t, err).Code != tc.code {
			t.Errorf("%s: %v", name, err)
		}
	}
}

func TestStatus(t *testing.T) {
	pending := []protocol.Pending{{Container: container, Account: "acme", ExpiresAt: "2026-10-17T13:00:00Z"}}
	tokens := &fakeTokens{pending: pending}
	got, err := Host{GitHub: tokens}.Status(context.Background())
	if err != nil || !slices.Equal(got.Revocations, pending) {
		t.Fatalf("%+v %v", got, err)
	}
	if !slices.Equal(tokens.calls, []string{"pending"}) {
		t.Fatalf("status performed maintenance: %v", tokens.calls)
	}
	if _, err := (Host{GitHub: &fakeTokens{retry: protocol.New(protocol.KeychainFailed, "x")}}).Status(context.Background()); protoErr(t, err).Code != protocol.KeychainFailed {
		t.Fatal(err)
	}
}

func TestCleanup(t *testing.T) {
	pending := []protocol.Pending{{Container: container, Account: "acme", ExpiresAt: "2026-10-17T13:00:00Z"}}
	tokens := &fakeTokens{pending: pending}
	got, err := (Host{GitHub: tokens}).Cleanup(context.Background())
	if err != nil || !slices.Equal(got.Revocations, pending) || !slices.Equal(tokens.calls, []string{"retry"}) {
		t.Fatalf("%+v %v %v", got, err, tokens.calls)
	}
	if _, err := (Host{GitHub: &fakeTokens{retry: protocol.New(protocol.KeychainFailed, "x")}}).Cleanup(context.Background()); protoErr(t, err).Code != protocol.KeychainFailed {
		t.Fatal(err)
	}
}
