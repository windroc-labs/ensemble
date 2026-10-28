package launcher

import (
	"context"
	"encoding/json"
	"errors"
	"maps"
	"slices"
	"strings"
	"testing"

	"ensemble/broker/internal/credential"
	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/protocol"
)

var expiry = []protocol.Expiry{{Account: "acme", ExpiresAt: "2026-10-17T13:00:00Z"}}

// fakeTokens records the launcher's Tokens calls; each err field fails its method. An issued set's revocation is the
// call "revoke <container>"; close's Revoke is "read-revoke <container>", reading the token files when readFiles.
type fakeTokens struct {
	prepared                    [][]byte
	owners                      []string
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

// fixtureSet is fixture manifest-github-app's permissions.
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
func (f *fakeTokens) Issue(_ context.Context, app, repo string, permissions map[string]string, c string) ([]credential.Command, func(), []protocol.Expiry, error) {
	f.calls, f.permissions, f.repos = append(f.calls, "issue "+app+" "+c), append(f.permissions, permissions), append(f.repos, repo)
	if f.issue != nil {
		return nil, nil, nil, f.issue
	}
	owners := f.owners
	if len(owners) == 0 {
		owners = []string{"acme"}
	}
	var commands []credential.Command
	for _, owner := range owners {
		data := []byte("ghs_token")
		f.prepared = append(f.prepared, data)
		commands = append(commands, credential.Command{Argv: []string{"write", owner}, Stdin: data})
	}
	return commands, func() {
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

func githubManifest(t *testing.T) string { return fixtureManifest(t, "manifest-github-app") }

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
	bot := "ensemble-broker-bot[bot]"
	email := "343764394+ensemble-broker-bot[bot]@users.noreply.github.com"
	expected := map[string]string{"ENSEMBLE_PR_AUTHOR": bot, "GIT_AUTHOR_NAME": bot, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": bot, "GIT_COMMITTER_EMAIL": email}
	if !maps.Equal(got.Env, expected) {
		t.Fatalf("bot env: %v", got.Env)
	}
	commands := startCommands(t, d)
	if len(commands) != 2 || commands[0].Argv[0] != "inject" || !slices.Equal(commands[1].Argv, []string{"write", "acme"}) || string(commands[1].Stdin) != "ghs_token" {
		t.Fatalf("credential order or bytes: %+v", commands)
	}
}

func TestStartGitHubNull(t *testing.T) {
	tokens := &fakeTokens{}
	h, _ := githubHost(&world{labels: goodLabels()}, tokens)
	got, err := h.Start(context.Background(), validManifest(t))
	if err != nil || got.Deadline != nil || len(got.Env) != 0 || len(tokens.calls) != 0 {
		t.Fatalf("%+v %v %q", got, err, tokens.calls)
	}
	if out, _ := json.Marshal(got); !strings.Contains(string(out), `"env":{}`) {
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
	path := fixtureManifest(t, "manifest-github-app", func(f map[string]any) {
		withGitHubGrant(t, f, map[string]any{"app": "ensemble-broker-bot", "permissions": fixtureSet, "repo": "acme/out"})
	})
	tokens := &fakeTokens{}
	h := mountHost()
	h.Docker, h.GitHub = &fakeDocker{}, tokens
	if _, err := h.Maintain(context.Background(), container, path, nil, false); err != nil {
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
	host := mountHost()
	host.Docker, host.GitHub = &fakeDocker{}, &fakeTokens{refresh: docker.ErrUnreachable}
	got, err = host.Maintain(context.Background(), container, githubManifest(t), nil, false)
	if e := results(got)["github"].Error; err != nil || len(got.Results) != 1 || e == nil || e.Code != protocol.DockerUnreachable {
		t.Errorf("unreachable: %+v %v", got, err)
	}
}
