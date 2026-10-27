package broker

import (
	"context"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"ensemble/broker/internal/audit"
	"ensemble/broker/internal/docker"
)

// files is a read of the token files answering out.
func files(out string) func(context.Context, []string) ([]byte, error) {
	return func(context.Context, []string) ([]byte, error) { return []byte(out), nil }
}

// shortTimeout makes each in-command deadline d for t.
func shortTimeout(t *testing.T, d time.Duration) {
	t.Helper()
	was := revocationTimeout
	revocationTimeout = d
	t.Cleanup(func() { revocationTimeout = was })
}

// AC6: close reads the container's token files back and revokes each installation token in them once.
func TestRevokeReadsTokenFiles(t *testing.T) {
	now := clock.Unix()
	line := func(name, token string) string { return fmt.Sprintf("%s %d %s\n", name, now-60, token) }
	unreadable := []string{audit.Unrevoked + " token files unreadable  "}
	for name, tc := range map[string]struct {
		out     string
		err     error
		revoked []string
		audited []string // result, reason, account and expiry ("": null) of each line
	}{
		"two files": {line("acme", "ghs_A1") + line("Other-Org", "ghs_B2"), nil, []string{"ghs_A1", "ghs_B2"},
			[]string{"revoked  acme 2026-10-17T12:59:00Z", "revoked  Other-Org 2026-10-17T12:59:00Z"}},
		"no token files": {"", nil, nil, nil},
		"unparseable lines skipped": {"garbage\n" + line("bad/name", "ghs_X1") + "acme soon ghs_X2\nacme 12 \n" + line("acme", "ghs_X3 more") +
			line(".acme.new", "ghs_X4") + "acme -12 ghs_X5\n" + line("acme", "ghs_OK1"), nil, []string{"ghs_OK1"},
			[]string{"revoked  acme 2026-10-17T12:59:00Z"}},
		"duplicate and other shapes not sent": {line("acme", "ghs_D1") + line("other", "ghs_D1") + line("a", "ghp_PERSONAL1") +
			line("b", "gho_OAUTH1") + line("c", "plaintoken") + line("d", "ghs_with_underscore") + line("e", "ghs_"), nil,
			[]string{"ghs_D1"}, []string{"revoked  acme 2026-10-17T12:59:00Z"}},
		"more than 100 lines": {strings.Repeat(line("acme", "ghs_F1"), 101), nil, nil, unreadable},
		"absent container":    {"", &docker.CommandError{Exit: 1, Stderr: "Error response from daemon: No such container: " + ctr}, nil, nil},
		"read fails":          {"", &docker.CommandError{Exit: 1, Stderr: "container is not running"}, nil, unreadable},
		"future mtime clamped": {fmt.Sprintf("acme %d ghs_T1\n", now+5*3600), nil, []string{"ghs_T1"},
			[]string{"revoked  acme 2026-10-17T13:00:00Z"}},
	} {
		t.Run(name, func(t *testing.T) {
			r := newRig(t)
			rec, sink := r.audited("close", ctr)
			var argv []string
			read := func(_ context.Context, a []string) ([]byte, error) { argv = a; return []byte(tc.out), tc.err }
			r.g.Revoke(context.Background(), ctr, read)
			if !slices.Equal(argv, []string{"/bin/sh", "-c", tokensScript, "sh", "/run/ensemble/secrets/github"}) {
				t.Fatalf("read %q", argv)
			}
			if !slices.Equal(r.api.revoked, tc.revoked) {
				t.Fatalf("revoked %q, want %q", r.api.revoked, tc.revoked)
			}
			var got []string
			for _, c := range flush(t, rec, sink)[0].Credentials {
				if c.Kind != audit.GitHubToken || c.Installation != 0 {
					t.Fatalf("%+v", c)
				}
				got = append(got, strings.Join([]string{c.Result, c.Reason, c.Account, c.ExpiresAt}, " "))
			}
			if !slices.Equal(got, tc.audited) {
				t.Fatalf("audit %q, want %q", got, tc.audited)
			}
			r.noKeychain(t)
		})
	}
}

// The read has its own deadline, whatever the caller's context: one that hangs ends there, revoking nothing.
func TestRevokeReadHangs(t *testing.T) {
	shortTimeout(t, 50*time.Millisecond)
	r := newRig(t)
	rec, sink := r.audited("close", ctr)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	var live bool
	read := func(ctx context.Context, _ []string) ([]byte, error) {
		_, deadline := ctx.Deadline()
		live = ctx.Err() == nil && deadline
		<-ctx.Done()
		return nil, ctx.Err()
	}
	began := time.Now()
	r.g.Revoke(ctx, ctr, read)
	if !live || time.Since(began) > 5*time.Second {
		t.Fatalf("read context live %v, took %v", live, time.Since(began))
	}
	got := flush(t, rec, sink)[0]
	if !slices.Equal(resultsOf(got), []string{audit.Unrevoked}) || got.Credentials[0].Reason != "token files unreadable" || len(r.api.revoked) != 0 {
		t.Fatalf("%+v", got)
	}
}

// One budget covers every revocation: against a GitHub that never answers, close sends one request and audits the
// rest unrevoked without any.
func TestRevokeSharesOneBudget(t *testing.T) {
	shortTimeout(t, 100*time.Millisecond)
	r := newRig(t)
	var mu sync.Mutex
	requests := 0
	r.g.API.HTTP = &http.Client{Transport: roundTrip(func(req *http.Request) (*http.Response, error) {
		mu.Lock()
		requests++
		mu.Unlock()
		<-req.Context().Done()
		return nil, req.Context().Err()
	})}
	rec, sink := r.audited("close", ctr)
	var out strings.Builder
	for i := range 100 {
		fmt.Fprintf(&out, "acme %d ghs_H%d\n", clock.Unix(), i)
	}
	began := time.Now()
	r.g.Revoke(context.Background(), ctr, files(out.String()))
	if took := time.Since(began); took > 5*time.Second {
		t.Fatalf("took %v", took)
	}
	got := flush(t, rec, sink)[0]
	if len(got.Credentials) != 100 || requests != 1 {
		t.Fatalf("%d lines, %d requests", len(got.Credentials), requests)
	}
	for _, c := range got.Credentials[1:] {
		if c.Result != audit.Unrevoked || c.Reason != context.DeadlineExceeded.Error() {
			t.Fatalf("%+v", c)
		}
	}
}

// TestTokensScriptFollowsNoSymlink runs the shipped tokensScript: a regular token file is listed with its mtime and
// value; a symlinked file, anything under a symlinked directory, a dot file and a symlinked token directory are not;
// a missing directory lists nothing and succeeds.
func TestTokensScriptFollowsNoSymlink(t *testing.T) {
	dir, outside := t.TempDir(), t.TempDir()
	write := func(path, value string) {
		t.Helper()
		if err := os.WriteFile(path, []byte(value), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	write(filepath.Join(dir, "acme"), "ghs_REAL1")
	write(filepath.Join(dir, ".acme.new"), "ghs_TEMP1")
	write(filepath.Join(outside, "target"), "ghs_LINKED1")
	for link, target := range map[string]string{"linked": filepath.Join(outside, "target"), "linkdir": outside} {
		if err := os.Symlink(target, filepath.Join(dir, link)); err != nil {
			t.Fatal(err)
		}
	}
	linkedDir := filepath.Join(t.TempDir(), "github")
	if err := os.Symlink(dir, linkedDir); err != nil {
		t.Fatal(err)
	}
	run := func(arg string) string {
		t.Helper()
		out, err := exec.Command("/bin/sh", "-c", tokensScript, "sh", arg).Output()
		if err != nil {
			t.Fatalf("%s: %v", arg, err)
		}
		return string(out)
	}
	got := strings.Split(strings.TrimSuffix(run(dir), "\n"), "\n")
	if len(got) != 1 {
		t.Fatalf("listed %q", got)
	}
	fields := strings.SplitN(got[0], " ", 3)
	if len(fields) != 3 || fields[0] != "acme" || fields[2] != "ghs_REAL1" {
		t.Fatalf("listed %q", got)
	}
	if runtime.GOOS == "linux" && !regexp.MustCompile(`^[0-9]+$`).MatchString(fields[1]) {
		t.Fatalf("mtime %q", fields[1])
	}
	for _, arg := range []string{linkedDir, filepath.Join(dir, "missing")} {
		if out := run(arg); out != "" {
			t.Fatalf("%s listed %q", arg, out)
		}
	}
}
