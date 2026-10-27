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

// fakeTokens records the launcher's Tokens calls; each err field fails its method. An issued set's revocation is the
// call "revoke <container>"; close's Revoke is "read-revoke <container>", reading the token files when readFiles.
type fakeTokens struct {
	calls                       []string
	ready, user, issue, refresh error
	prefetch                    error
	onRevoke                    func()
	readFiles                   bool
	readOut                     []byte
	readErr                     error
	permissions                 []map[string]string // each Issue's and Refresh's set
	repos                       []string            // each Issue's and Refresh's repo
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
func (f *fakeTokens) Issue(_ context.Context, app, repo string, permissions map[string]string, c string, exec func([]string, []byte) error) (func(), []protocol.Expiry, error) {
	f.calls, f.permissions, f.repos = append(f.calls, "issue "+app+" "+c), append(f.permissions, permissions), append(f.repos, repo)
	if f.issue != nil {
		return nil, nil, f.issue
	}
	if err := exec([]string{"write"}, []byte("ghs_token")); err != nil {
		return nil, nil, err
	}
	return func() {
		f.calls = append(f.calls, "revoke "+c)
		if f.onRevoke != nil {
			f.onRevoke()
		}
	}, expiry, nil
}
func (f *fakeTokens) Refresh(_ context.Context, app, repo string, permissions map[string]string, c string, exec func([]string, []byte) error) ([]protocol.Expiry, error) {
	f.calls, f.permissions, f.repos = append(f.calls, "refresh "+app+" "+c), append(f.permissions, permissions), append(f.repos, repo)
	if f.refresh != nil {
		return nil, f.refresh
	}
	return expiry, exec([]string{"write"}, []byte("ghs_token"))
}
func (f *fakeTokens) Revoke(ctx context.Context, c string, read func(context.Context, []string) ([]byte, error)) {
	f.calls = append(f.calls, "read-revoke "+c)
	if f.readFiles {
		f.readOut, f.readErr = read(ctx, []string{"/bin/sh", "-c", "tokens-script", "sh", "/run/ensemble/secrets/github"})
	}
}
func (f *fakeTokens) Prefetch(_ context.Context, repo, ref, dir, c string, run func([]string, []byte) error) error {
	f.calls = append(f.calls, "prefetch "+repo+" "+ref+" "+dir+" "+c)
	if f.prefetch != nil {
		return f.prefetch
	}
	if err := run([]string{"/bin/sh", "-c", "clone-script", "sh", repo, ref, dir}, []byte("ghs_prefetch")); err != nil {
		return protocol.New(protocol.StartFailed, "Prefetching "+repo+" at "+ref+" failed.")
	}
	return nil
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
	if got.Deadline != nil || !slices.Equal(tokens.calls, []string{"ready ensemble-broker-bot", "user ensemble-broker-bot", "issue ensemble-broker-bot " + container}) {
		t.Fatalf("%+v %q", got, tokens.calls)
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
	if err != nil || got.Deadline != nil || got.GitHubUserID != nil || len(tokens.calls) != 0 {
		t.Fatalf("%+v %v %q", got, err, tokens.calls)
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
			if tokens := h.GitHub.(*fakeTokens); slices.ContainsFunc(tokens.calls, func(c string) bool { return strings.HasPrefix(c, "revoke") }) {
				t.Fatalf("revoked a set Issue already rolled back: %q", tokens.calls)
			}
		})
	}
}

// A repo grant's renewal names the manifest's repo, so the broker renews that repository's slot alone.
func TestMaintainRepoGrant(t *testing.T) {
	data, err := os.ReadFile(filepath.Join(fixtures.Dir(), "manifest-github-app.json"))
	if err != nil {
		t.Fatal(err)
	}
	data = []byte(strings.Replace(string(data), `"app": "ensemble-broker-bot",`, `"app": "ensemble-broker-bot", "repo": "acme/out",`, 1))
	tokens := &fakeTokens{}
	h := mountHost()
	h.Docker, h.GitHub = &fakeDocker{}, tokens
	if _, err := h.Maintain(context.Background(), container, writeManifest(t, data), nil, false); err != nil {
		t.Fatal(err)
	}
	if !slices.Contains(tokens.calls, "refresh ensemble-broker-bot "+container) || !slices.Equal(tokens.repos, []string{"acme/out"}) {
		t.Fatalf("token calls %q repos %q", tokens.calls, tokens.repos)
	}
}

func TestMaintainGitHub(t *testing.T) {
	tokens := &fakeTokens{}
	d := &fakeDocker{}
	h := mountHost()
	h.Docker, h.GitHub = d, tokens
	got, err := h.Maintain(context.Background(), container, githubManifest(t), nil, false)
	if err != nil || got.Container != container || len(got.Results) != 1 || got.Results[0].Credential != "github" ||
		*got.Results[0].ExpiresAt != expiry[0].ExpiresAt || got.Deadline != nil {
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
		code                protocol.Code
	}{
		"other container": {other, githubManifest(t), protocol.ManifestRejected},
		"missing":         {container, "/nonexistent/m.json", protocol.ManifestRejected},
	} {
		host := mountHost()
		host.Docker, host.GitHub = &fakeDocker{}, &fakeTokens{}
		_, err := host.Maintain(context.Background(), tc.container, tc.manifest, nil, false)
		if protoErr(t, err).Code != tc.code {
			t.Errorf("%s: %v", name, err)
		}
	}
	for name, tc := range map[string]struct {
		tokens *fakeTokens
		code   protocol.Code
	}{
		"no key":      {&fakeTokens{ready: protocol.New(protocol.LoginRequired, "x")}, protocol.LoginRequired},
		"no record":   {&fakeTokens{refresh: protocol.New(protocol.RefreshFailed, "x")}, protocol.RefreshFailed},
		"write":       {&fakeTokens{refresh: errors.New("opaque")}, protocol.RefreshFailed},
		"unreachable": {&fakeTokens{refresh: docker.ErrUnreachable}, protocol.DockerUnreachable},
	} {
		host := mountHost()
		host.Docker, host.GitHub = &fakeDocker{}, tc.tokens
		got, err := host.Maintain(context.Background(), container, githubManifest(t), nil, false)
		if err != nil || len(got.Results) != 1 || got.Results[0].Error == nil || got.Results[0].Error.Code != tc.code {
			t.Errorf("%s: %+v %v", name, got, err)
		}
	}
	host := mountHost()
	host.Docker, host.GitHub = &fakeDocker{}, &fakeTokens{}
	if got, err := host.Maintain(context.Background(), container, validManifest(t), nil, false); err != nil || len(got.Results) != 0 {
		t.Errorf("github null: %+v %v", got, err)
	}
}
