package broker

import (
	"context"
	"encoding/json"
	"errors"
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
			if string(r.exec.stdin[0]) != "ghs_2_1" || !slices.Equal(r.api.bodies, []string{prefetchBody}) {
				t.Fatalf("stdin %q bodies %q", r.exec.stdin[0], r.api.bodies)
			}
			for _, a := range argv {
				if strings.Contains(a, "ghs_") {
					t.Fatalf("token in argv %q", argv)
				}
			}
			for _, p := range r.api.repoPaths {
				if strings.HasSuffix(p, "/installation Bearer ghs_2_1") || strings.Contains(p, "/git/ref/") && !strings.HasSuffix(p, "Bearer ghs_2_1") {
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

func TestPrefetchRevokes(t *testing.T) {
	r, rec := prefetchRig(t, installed())
	var during []string
	run := func(argv []string, stdin []byte) error {
		if got, ok := r.record(t); ok {
			for _, e := range got.Tokens {
				during = append(during, e.Token)
			}
		}
		return r.exec.run(argv, stdin)
	}
	if err := r.g.Prefetch(context.Background(), "acme/app", "main", "/w", ctr, run); err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(during, []string{"ghs_2_1"}) {
		t.Fatalf("recorded while cloning: %q", during)
	}
	if !slices.Equal(r.api.revoked, []string{"ghs_2_1"}) || len(r.backlog(t)) != 0 {
		t.Fatalf("revoked %q backlog %v", r.api.revoked, r.backlog(t))
	}
	if _, ok := r.record(t); ok {
		t.Fatal("record kept")
	}
	line := startLine(t, rec)
	if !strings.Contains(line, `"result":"issued"`) || !strings.Contains(line, `"result":"revoked"`) ||
		!strings.Contains(line, `"kind":"github-installation-token"`) || strings.Contains(line, "ghs_") {
		t.Fatalf("audit %s", line)
	}
}

func TestPrefetchRevokesAfterFailedClone(t *testing.T) {
	r, _ := prefetchRig(t, installed())
	r.exec.err = errors.New("exit status 128: fatal: could not read Password for ghs_2_1")
	err := r.g.Prefetch(context.Background(), "acme/app", "main", "/w", ctr, r.exec.run)
	wantCode(t, err, protocol.StartFailed)
	var e *protocol.Error
	errors.As(err, &e)
	if !strings.Contains(e.Message, "Prefetching acme/app at main failed.") || strings.Contains(e.Cause, "ghs_") || e.Cause == "" {
		t.Fatalf("%+v", e)
	}
	if !slices.Equal(r.api.revoked, []string{"ghs_2_1"}) {
		t.Fatalf("revoked %q", r.api.revoked)
	}
}

func TestPrefetchDockerUnreachable(t *testing.T) {
	r, _ := prefetchRig(t, installed())
	r.exec.err = docker.ErrUnreachable
	wantCode(t, r.g.Prefetch(context.Background(), "acme/app", "main", "/w", ctr, r.exec.run), protocol.DockerUnreachable)
	if !slices.Equal(r.api.revoked, []string{"ghs_2_1"}) {
		t.Fatalf("revoked %q", r.api.revoked)
	}
}

func TestPrefetchRevokeFailureQueues(t *testing.T) {
	r, rec := prefetchRig(t, installed())
	r.api.revokeErr = func(string) int { return 500 }
	if err := r.g.Prefetch(context.Background(), "acme/app", "main", "/w", ctr, r.exec.run); err != nil {
		t.Fatalf("a failed revocation failed the prefetch: %v", err)
	}
	if b := r.backlog(t); len(b) != 1 || b[0].Token != "ghs_2_1" || b[0].Container != ctr {
		t.Fatalf("backlog %+v", b)
	}
	if line := startLine(t, rec); !strings.Contains(line, `"result":"pending"`) {
		t.Fatalf("audit %s", line)
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
	if !slices.Equal(r.api.revoked, []string{"ghs_2_1"}) {
		t.Fatalf("revoked %q", r.api.revoked)
	}
}

func TestPrefetchRefLookupFails(t *testing.T) {
	repo := installed()
	repo.refStatus = 502
	r, _ := prefetchRig(t, repo)
	wantCode(t, r.g.Prefetch(context.Background(), "acme/app", "main", "/w", ctr, r.exec.run), protocol.GitHubFailed)
	if len(r.exec.argv) != 0 || !slices.Equal(r.api.revoked, []string{"ghs_2_1"}) {
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

func TestPrefetchUnrecordedTokenRevoked(t *testing.T) {
	r, _ := prefetchRig(t, installed())
	r.keys.putErr = errors.New("keychain full")
	wantCode(t, r.g.Prefetch(context.Background(), "acme/app", "main", "/w", ctr, r.exec.run), protocol.KeychainFailed)
	if len(r.exec.argv) != 0 || !slices.Equal(r.api.revoked, []string{"ghs_2_1"}) {
		t.Fatalf("runs=%d revoked %q", len(r.exec.argv), r.api.revoked)
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
