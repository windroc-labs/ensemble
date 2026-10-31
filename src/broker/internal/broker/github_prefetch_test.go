package broker

import (
	"context"
	"encoding/json"
	"errors"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"ensemble/broker/internal/audit"
	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/protocol"
)

const prefetchBody = `{"permissions":{"contents":"read"},"repositories":["app"]}`

type audited struct {
	rec  *audit.Recorder
	sink *lines
}

func prefetchRig(t *testing.T, repo *fakeRepo) (*rig, audited) {
	t.Helper()
	r := newRig(t, "other", "acme")
	r.api.repo = repo
	rec, sink := r.audited("start", ctr)
	return r, audited{rec, sink}
}

func installed() *fakeRepo {
	return &fakeRepo{installed: true, defaultBranch: "trunk", branches: []string{"main", "release/1.2", "both"}, tags: []string{"v1.0.0", "both"}}
}

func TestPrefetchResolvesRef(t *testing.T) {
	for ref, want := range map[string]string{"": "trunk", "main": "main", "release/1.2": "release/1.2", "v1.0.0": "v1.0.0", "both": "both"} {
		t.Run(ref, func(t *testing.T) {
			r, _ := prefetchRig(t, installed())
			if err := r.g.Prefetch(context.Background(), "acme/app", ref, "/w/project", ctr, r.exec.run); err != nil {
				t.Fatal(err)
			}
			if len(r.exec.argv) != 1 {
				t.Fatalf("runs %q", r.exec.argv)
			}
			argv := r.exec.argv[0]
			if !slices.Equal(argv, []string{"/bin/sh", "-c", cloneScript, "sh", "acme/app", want, "/w/project"}) {
				t.Fatalf("argv %q", argv)
			}
			if string(r.exec.stdin[0]) != tok(2, 1) || !slices.Equal(r.api.bodies, []string{prefetchBody}) {
				t.Fatalf("stdin %q bodies %q", r.exec.stdin[0], r.api.bodies)
			}
			for _, a := range argv {
				if strings.Contains(a, "ghs_") {
					t.Fatalf("token in argv %q", argv)
				}
			}
			for _, p := range r.api.repoPaths {
				if strings.HasSuffix(p, "/installation Bearer "+tok(2, 1)) || strings.Contains(p, "/git/ref/") && !strings.HasSuffix(p, "Bearer "+tok(2, 1)) {
					t.Fatalf("auth of %q", p)
				}
			}
		})
	}
}

// The clone script takes the token only from stdin, runs no repo hook and keeps no credential in the clone's config.
func TestCloneScript(t *testing.T) {
	for _, want := range []string{`cat > "$HOME/token"`, "core.hooksPath=/dev/null", "core.fsmonitor=false", "-c credential.helper= ",
		`clone --quiet --depth 1 --branch "$2" -- "https://github.com/$1.git" "$3"`} {
		if !strings.Contains(cloneScript, want) {
			t.Errorf("cloneScript lacks %q", want)
		}
	}
	if strings.Contains(cloneScript, "clone -c") || strings.Contains(cloneScript, "@github.com") {
		t.Error("cloneScript writes a credential into the clone")
	}
}

// The prefetch token is issued, audited, and revoked exactly once when the helper returns, or when the prefetch
// fails before it runs; it never reaches the Keychain.
func TestPrefetchRevokesOnceOnSuccessAndFailure(t *testing.T) {
	for name, tc := range map[string]struct {
		ref    string
		runErr error
		repo   func(*fakeRepo)
		code   protocol.Code
		runs   int
	}{
		"success":            {"main", nil, nil, "", 1},
		"helper fails":       {"main", errors.New("exit status 128: fatal: could not read Password for " + tok(2, 1)), nil, protocol.StartFailed, 1},
		"docker unreachable": {"main", docker.ErrUnreachable, nil, protocol.DockerUnreachable, 1},
		"ref check fails":    {"gone", nil, nil, protocol.ManifestRejected, 0},
		"ref lookup fails":   {"main", nil, func(f *fakeRepo) { f.refStatus = 502 }, protocol.GitHubFailed, 0},
	} {
		t.Run(name, func(t *testing.T) {
			repo := installed()
			if tc.repo != nil {
				tc.repo(repo)
			}
			r, a := prefetchRig(t, repo)
			r.exec.err = tc.runErr
			var during []string
			run := func(argv []string, stdin []byte) error {
				during = r.api.revokedTokens()
				return r.exec.run(argv, stdin)
			}
			err := r.g.Prefetch(context.Background(), "acme/app", tc.ref, "/w", ctr, run)
			if tc.code == "" && err != nil {
				t.Fatal(err)
			}
			if tc.code != "" {
				wantCode(t, err, tc.code)
				var e *protocol.Error
				if errors.As(err, &e) && strings.Contains(e.Message+e.Cause, "ghs_") {
					t.Fatalf("%+v", e)
				}
			}
			if len(r.exec.argv) != tc.runs || len(during) != 0 {
				t.Fatalf("runs %d, revoked while running %q", len(r.exec.argv), during)
			}
			if !slices.Equal(r.api.revoked, []string{tok(2, 1)}) {
				t.Fatalf("revoked %q", r.api.revoked)
			}
			r.noKeychain(t)
			line := startLine(t, a)
			if !strings.Contains(line, `"result":"issued"`) || !strings.Contains(line, `"result":"revoked"`) ||
				!strings.Contains(line, `"kind":"github-installation-token"`) || !strings.Contains(line, `"repositories":["acme/app"]`) {
				t.Fatalf("audit %s", line)
			}
		})
	}
}

func TestPrefetchNotInstalled(t *testing.T) {
	r, _ := prefetchRig(t, &fakeRepo{})
	err := r.g.Prefetch(context.Background(), "acme/app", "main", "/w", ctr, r.exec.run)
	wantCode(t, err, protocol.GitHubFailed)
	if !strings.Contains(err.Error(), "not installed on acme/app") || r.api.issued != 0 || len(r.exec.argv) != 0 {
		t.Fatalf("%v issued=%d runs=%d", err, r.api.issued, len(r.exec.argv))
	}
}

func TestPrefetchUnknownRef(t *testing.T) {
	r, _ := prefetchRig(t, installed())
	err := r.g.Prefetch(context.Background(), "acme/app", "gone", "/w", ctr, r.exec.run)
	wantCode(t, err, protocol.ManifestRejected)
	if !strings.Contains(err.Error(), "prefetch ref gone is no branch or tag of acme/app") || len(r.exec.argv) != 0 {
		t.Fatalf("%v runs=%d", err, len(r.exec.argv))
	}
	if !slices.Equal(r.api.revoked, []string{tok(2, 1)}) {
		t.Fatalf("revoked %q", r.api.revoked)
	}
}

func TestPrefetchRefLookupFails(t *testing.T) {
	repo := installed()
	repo.refStatus = 502
	r, _ := prefetchRig(t, repo)
	wantCode(t, r.g.Prefetch(context.Background(), "acme/app", "main", "/w", ctr, r.exec.run), protocol.GitHubFailed)
	if len(r.exec.argv) != 0 || !slices.Equal(r.api.revoked, []string{tok(2, 1)}) {
		t.Fatalf("runs=%d revoked %q", len(r.exec.argv), r.api.revoked)
	}
}

func TestPrefetchUnsafeDefaultBranch(t *testing.T) {
	repo := installed()
	repo.defaultBranch = "--upload-pack=x"
	r, _ := prefetchRig(t, repo)
	wantCode(t, r.g.Prefetch(context.Background(), "acme/app", "", "/w", ctr, r.exec.run), protocol.GitHubFailed)
	if len(r.exec.argv) != 0 {
		t.Fatalf("runs %q", r.exec.argv)
	}
}

func TestPrefetchNoLogin(t *testing.T) {
	r, _ := prefetchRig(t, installed())
	r.g.Apps = keys{err: errors.New("none")}
	wantCode(t, r.g.Prefetch(context.Background(), "acme/app", "main", "/w", ctr, r.exec.run), protocol.LoginRequired)
	if r.api.issued != 0 {
		t.Fatal("issued without a login")
	}
}

// startLine is a's flushed start line for ctr, as JSON text; flush fails on a token in it.
func startLine(t *testing.T, a audited) string {
	t.Helper()
	got := flush(t, a.rec, a.sink)
	if len(got) != 1 || got[0].Container != ctr {
		t.Fatalf("lines %+v", got)
	}
	data, err := json.Marshal(got[0])
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

// TestCloneScriptLeavesNoCredential runs cloneScript for real against a local repo standing in for github.com: the
// clone holds the ref and no credential anywhere, its remote URL included.
func TestCloneScriptLeavesNoCredential(t *testing.T) {
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("no git")
	}
	tmp := t.TempDir()
	env := append(os.Environ(), "HOME="+filepath.Join(tmp, "home"), "GIT_CONFIG_NOSYSTEM=1", "GIT_CONFIG_GLOBAL=/dev/null",
		"GIT_AUTHOR_NAME=t", "GIT_AUTHOR_EMAIL=t@example.com", "GIT_COMMITTER_NAME=t", "GIT_COMMITTER_EMAIL=t@example.com")
	git := func(dir string, args ...string) string {
		t.Helper()
		cmd := exec.Command("git", append([]string{"-C", dir}, args...)...)
		cmd.Env = env
		out, err := cmd.CombinedOutput()
		if err != nil {
			t.Fatalf("git %q: %v %s", args, err, out)
		}
		return strings.TrimSpace(string(out))
	}
	src, bare, ws := filepath.Join(tmp, "src"), filepath.Join(tmp, "bare.git"), filepath.Join(tmp, "ws")
	for _, dir := range []string{src, ws, filepath.Join(tmp, "home")} {
		if err := os.MkdirAll(dir, 0o700); err != nil {
			t.Fatal(err)
		}
	}
	git(src, "init", "-q", "-b", "main")
	if err := os.WriteFile(filepath.Join(src, "a.txt"), []byte("main\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	git(src, "add", "a.txt")
	git(src, "commit", "-q", "-m", "one")
	git(src, "branch", "release/1.2")
	git(tmp, "clone", "-q", "--bare", src, bare)
	const secret = "ghs_prefetchsecret123"
	cmd := exec.Command("/bin/sh", "-c", cloneScript, "sh", "acme/app", "release/1.2", ws)
	cmd.Env = append(env, "GIT_CONFIG_COUNT=1", "GIT_CONFIG_KEY_0=url.file://"+bare+".insteadOf", "GIT_CONFIG_VALUE_0=https://github.com/acme/app.git")
	cmd.Stdin = strings.NewReader(secret)
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("clone: %v %s", err, out)
	}
	if got := git(ws, "rev-parse", "--abbrev-ref", "HEAD"); got != "release/1.2" {
		t.Fatalf("HEAD %q", got)
	}
	if got := git(ws, "config", "--get", "remote.origin.url"); got != "https://github.com/acme/app.git" {
		t.Fatalf("remote %q", got)
	}
	err := filepath.WalkDir(ws, func(path string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		data, err := os.ReadFile(path)
		if err == nil && strings.Contains(string(data), secret) {
			t.Errorf("token in %s", path)
		}
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
}
