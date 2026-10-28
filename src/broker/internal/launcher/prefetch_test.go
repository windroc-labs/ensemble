package launcher

import (
	"context"
	"errors"
	"maps"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/protocol"
)

// prefetchHost is credentialHost with a real Home holding a default-base variant record for imageKey.
func prefetchHost(t *testing.T, w *world) (Host, *fakeDocker, *fakeTokens) {
	t.Helper()
	h, d, _, tokens, _, _, _ := credentialHost(w)
	h.Home = t.TempDir()
	putFile(t, h.recordPath(imageKey), `{"package":"/p","base_image":"","plugins":[],"image_plugins":[]}`)
	return h, d, tokens
}

// prefetchManifest is manifest-valid.json for h's Home with prefetch acme/app at main, edit applied last.
func prefetchManifest(t *testing.T, h Host, edit func(map[string]any)) string {
	t.Helper()
	return editedManifest(t, func(f map[string]any) {
		f["claude_dir"] = h.Home + "/.ensemble/docker-state/worker_1/claude"
		f["prefetch"] = map[string]any{"repo": "acme/app", "ref": "main"}
		if edit != nil {
			edit(f)
		}
	})
}

// helperRun is the prefetch helper's docker run, nil when none ran.
func helperRun(d *fakeDocker) ([]string, []byte) {
	for i, c := range d.calls {
		if c[0] == "run" && slices.Contains(c, "ensemble.prefetch=1") {
			return c, d.stdin[i]
		}
	}
	return nil, nil
}

func callIndex(d *fakeDocker, match func([]string) bool) int {
	return slices.IndexFunc(d.calls, match)
}

func TestPrefetchHelperArgs(t *testing.T) {
	w := &world{labels: goodLabels()}
	h, d, tokens := prefetchHost(t, w)
	path := prefetchManifest(t, h, func(f map[string]any) { f["gateway"] = "mini-op" })
	if _, err := h.Start(context.Background(), path); err != nil {
		t.Fatal(err)
	}
	if !slices.Contains(tokens.calls, "prefetch acme/app main "+home+"/src/project "+container) {
		t.Fatalf("token calls %q", tokens.calls)
	}
	run, stdin := helperRun(d)
	if run == nil {
		t.Fatalf("no helper in %q", d.verbs())
	}
	if string(stdin) != "ghs_prefetch" {
		t.Fatalf("stdin %q", stdin)
	}
	for _, a := range run {
		if strings.Contains(a, "ghs_") {
			t.Fatalf("token in argv %q", run)
		}
	}
	if got := networkArgs(run); !slices.Equal(got, []string{workersNetwork}) {
		t.Fatalf("helper networks %q", got)
	}
	env := envArgs(run)
	if env["HTTPS_PROXY"] != workersProxy || env["HOME"] != "/run/prefetch" || env["GIT_CONFIG_NOSYSTEM"] != "1" || len(env) != 8 {
		t.Fatalf("helper env %v", env)
	}
	for k, v := range map[string]string{"--pull": "never", "--user": "1001:1002", "--cap-drop": "ALL", "--security-opt": "no-new-privileges",
		"--tmpfs": "/run/prefetch:rw,noexec,nosuid,nodev,size=65536,mode=0700,uid=1001,gid=1002",
		"--mount": "type=volume,source=" + volume + ",target=" + home + "/src/project,volume-nocopy", "--entrypoint": "/bin/sh"} {
		if flag(run, k) != v {
			t.Errorf("helper %s=%q in %q", k, flag(run, k), run)
		}
	}
	if !slices.Contains(run, "--rm") || !slices.Contains(run, "--interactive") || !strings.HasPrefix(flag(run, "--name"), "ensemble-prefetch-") {
		t.Fatalf("helper %q", run)
	}
	if i := slices.Index(run, digest); i < 0 || !slices.Equal(run[i+1:], []string{"-c", "clone-script", "sh", "acme/app", "main", home + "/src/project"}) {
		t.Fatalf("helper image and argv %q", run)
	}
	// The run container stays on the manifest's network.
	if got := networkArgs(detached(d)); !slices.Equal(got, []string{miniOpNetwork}) {
		t.Fatalf("run networks %q", got)
	}
	// One proxy inspection serves both networks.
	if n := len(slices.DeleteFunc(slices.Clone(d.calls), func(c []string) bool { return !slices.Contains(c, proxyID) })); n != 1 {
		t.Fatalf("proxy inspected %d times: %q", n, d.verbs())
	}
}

func TestPrefetchSkippedForExistingWorkspace(t *testing.T) {
	w := &world{labels: goodLabels(), volume: "matching"}
	h, d, tokens := prefetchHost(t, w)
	path := prefetchManifest(t, h, func(f map[string]any) { f["gateway"] = "mini-op" })
	if _, err := h.Start(context.Background(), path); err != nil {
		t.Fatal(err)
	}
	if run, _ := helperRun(d); run != nil || slices.ContainsFunc(tokens.calls, func(c string) bool { return strings.HasPrefix(c, "prefetch") }) {
		t.Fatalf("prefetched into an existing workspace: %q %q", run, tokens.calls)
	}
	for _, c := range d.calls {
		if c[0] == "network" && c[2] == workersNetwork {
			t.Fatalf("looked up workers: %q", c)
		}
	}
}

// A volume a racing start created carries that start's prefetch-start label: not this start's to prefetch or remove.
func TestPrefetchRacingCreate(t *testing.T) {
	w := &world{labels: goodLabels(), otherNonce: true}
	h, d, tokens := prefetchHost(t, w)
	h.GitHub.(*fakeTokens).prefetch = errors.New("must not run")
	if _, err := h.Start(context.Background(), prefetchManifest(t, h, nil)); err != nil {
		t.Fatal(err)
	}
	if run, _ := helperRun(d); run != nil || hasCall(d, "volume rm") || slices.Contains(d.verbs(), "run --rm") {
		t.Fatalf("calls %q tokens %q", d.verbs(), tokens.calls)
	}
}

func TestPrefetchVolumeNonce(t *testing.T) {
	w := &world{labels: goodLabels()}
	h, d, _ := prefetchHost(t, w)
	if _, err := h.Start(context.Background(), prefetchManifest(t, h, nil)); err != nil {
		t.Fatal(err)
	}
	i := callIndex(d, func(c []string) bool { return c[0] == "volume" && c[1] == "create" })
	if i < 0 || len(w.nonce) != 32 || !slices.Contains(d.calls[i], "ensemble.prefetch-start="+w.nonce) {
		t.Fatalf("create %q nonce %q", d.calls[i], w.nonce)
	}
	// Without prefetch, the create is unchanged.
	w2 := &world{labels: goodLabels()}
	h2, d2, _ := prefetchHost(t, w2)
	path := editedManifest(t, func(f map[string]any) { f["claude_dir"] = h2.Home + "/.ensemble/docker-state/worker_1/claude" })
	if _, err := h2.Start(context.Background(), path); err != nil {
		t.Fatal(err)
	}
	create := []string{"volume", "create", "--label", "ensemble.kind=ws", "--label", "ensemble.workspace=" + home + "/src/project", volume}
	if i := callIndex(d2, func(c []string) bool { return c[0] == "volume" && c[1] == "create" }); !slices.Equal(d2.calls[i], create) {
		t.Fatalf("create %q", d2.calls[i])
	}
}

func TestPrefetchOrder(t *testing.T) {
	lw := newLockWorld()
	w := &world{labels: goodLabels()}
	h, base, _ := prefetchHost(t, w)
	d := &fakeDocker{respond: func(args []string, stdin []byte) ([]byte, error) {
		if out, err, handled := lw.respond(args); handled {
			return out, err
		}
		return base.respond(args, stdin)
	}}
	h.Docker = d
	h.Now = func() time.Time { return time.Unix(1000, 0) }
	path := prefetchManifest(t, h, func(f map[string]any) { f["lock"], f["lock_owner"] = "dummy", "worker_1" })
	if _, err := h.Start(context.Background(), path); err != nil {
		t.Fatal(err)
	}
	at := func(match func([]string) bool) int {
		i := callIndex(d, match)
		if i < 0 {
			t.Fatalf("missing call in %q", d.verbs())
		}
		return i
	}
	order := []int{
		at(func(c []string) bool { return c[0] == "container" && c[2] == proxyID }),
		at(func(c []string) bool { return c[0] == "image" && c[1] == "inspect" }),
		at(func(c []string) bool { return c[0] == "container" && c[2] == container }),
		at(func(c []string) bool { return c[0] == "volume" && c[1] == "create" }),
		at(func(c []string) bool { return c[0] == "run" && c[1] == "--rm" && slices.Contains(c, "CHOWN") }),
		at(func(c []string) bool { return c[0] == "run" && slices.Contains(c, "ensemble.prefetch=1") }),
		at(func(c []string) bool { return c[0] == "create" && flag(c, "--name") == "windroc-lock-dummy" }),
		at(func(c []string) bool { return c[0] == "run" && c[1] == "--detach" }),
	}
	if !slices.IsSorted(order) {
		t.Fatalf("order %v in %q", order, d.verbs())
	}
}

func TestPrefetchFailures(t *testing.T) {
	for _, tc := range []struct {
		name  string
		setup func(*world, Host)
		code  protocol.Code
		want  string
	}{
		{"helper fails", func(w *world, _ Host) {
			w.prefetchErr = &docker.CommandError{Exit: 128, Stderr: "fatal: Remote branch main not found"}
		}, protocol.StartFailed, "Prefetching acme/app at main failed."},
		{"github refuses", func(_ *world, h Host) {
			h.GitHub.(*fakeTokens).prefetch = protocol.New(protocol.GitHubFailed, "GitHub App ensemble-broker-bot is not installed on acme/app")
		}, protocol.GitHubFailed, "not installed on acme/app"},
		{"no default image", func(_ *world, h Host) {
			_ = os.Remove(h.recordPath(imageKey))
			_ = os.WriteFile(h.recordPath(strings.Repeat("c", 64)), []byte(`{"package":"/p","base_image":"dummy-env:test"}`), 0o600)
		}, protocol.StartFailed, "No default worker image to prefetch with; run ensemble-broker build"},
		{"workers network missing", func(w *world, _ Host) {
			w.proxy = labelsEdit(func(l map[string]any) { l["windroc.egress.networks"] = "mini-op" })
		}, protocol.EgressUnavailable, "has no valid workers network labels"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			w := &world{labels: goodLabels()}
			h, d, tokens := prefetchHost(t, w)
			tc.setup(w, h)
			path := prefetchManifest(t, h, func(f map[string]any) { f["gateway"] = "mini-op" })
			_, err := h.Start(context.Background(), path)
			e := protoErr(t, err)
			if e.Code != tc.code || !strings.Contains(e.Message, tc.want) {
				t.Fatalf("%+v", e)
			}
			if detached(d) != nil || !hasCall(d, "volume rm") || w.volume != "" {
				t.Fatalf("calls %q", d.verbs())
			}
			if slices.ContainsFunc(tokens.calls, func(c string) bool { return strings.HasPrefix(c, "issue") }) {
				t.Fatalf("token calls %q", tokens.calls)
			}
		})
	}
}

func TestPrefetchHelperFailureRemovesHelper(t *testing.T) {
	w := &world{labels: goodLabels(), prefetchErr: &docker.CommandError{Exit: 128, Stderr: "boom"}}
	h, d, _ := prefetchHost(t, w)
	_, _ = h.Start(context.Background(), prefetchManifest(t, h, nil))
	run, _ := helperRun(d)
	name := flag(run, "--name")
	if !slices.ContainsFunc(d.calls, func(c []string) bool { return slices.Equal(c, []string{"rm", "--force", name}) }) {
		t.Fatalf("helper %s not removed: %q", name, d.verbs())
	}
}

func TestPrefetchBusyRemovesWorkspace(t *testing.T) {
	for _, tc := range []struct {
		name  string
		setup func(*lockWorld)
	}{
		{"holder", func(lw *lockWorld) {
			lw.add("other", "running", map[string]string{"windroc.lock": "dummy", "windroc.lock.owner": "other-session"})
		}},
		{"mutex", func(lw *lockWorld) {
			lw.add("windroc-lock-dummy", "created", map[string]string{"windroc.lock.mutex": "dummy", "windroc.lock.since": "1000"})
		}},
		{"recheck", func(lw *lockWorld) {
			lw.after = func(_ string, args []string, _ []byte) {
				if args[0] == "run" && args[1] == "--detach" {
					lw.add("other", "running", map[string]string{"windroc.lock": "dummy", "windroc.lock.owner": "other-session"})
					lw.after = nil
				}
			}
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			lw := newLockWorld()
			w := &world{labels: goodLabels()}
			h, base, _ := prefetchHost(t, w)
			d := &fakeDocker{respond: func(args []string, stdin []byte) ([]byte, error) {
				out, err, handled := lw.respond(args)
				if handled {
					if lw.after != nil {
						lw.after("", args, out)
					}
					return out, err
				}
				return base.respond(args, stdin)
			}}
			h.Docker = d
			h.Now = func() time.Time { return time.Unix(1000, 0) }
			tc.setup(lw)
			path := prefetchManifest(t, h, func(f map[string]any) { f["lock"], f["lock_owner"] = "dummy", "worker_1" })
			_, err := h.Start(context.Background(), path)
			if e := protoErr(t, err); e.Code != protocol.Busy {
				t.Fatalf("%+v", e)
			}
			rm := callIndex(d, func(c []string) bool { return c[0] == "volume" && c[1] == "rm" })
			if rm < 0 || w.volume != "" {
				t.Fatalf("workspace kept: %q", d.verbs())
			}
			if run, _ := helperRun(d); run == nil {
				t.Fatal("no prefetch")
			}
		})
	}
}

func TestPrefetchWorkspaceRemovalFailure(t *testing.T) {
	w := &world{labels: goodLabels(), prefetchErr: &docker.CommandError{Exit: 1, Stderr: "boom"},
		volumeRmErr: &docker.CommandError{Exit: 1, Stderr: "volume is in use"}}
	h, _, _ := prefetchHost(t, w)
	_, err := h.Start(context.Background(), prefetchManifest(t, h, nil))
	if e := protoErr(t, err); !strings.HasSuffix(e.Message, "Remove it with docker volume rm "+volume+" before retrying.") {
		t.Fatalf("%+v", e)
	}
}

func TestPrefetchNoGitHubTokens(t *testing.T) {
	w := &world{labels: goodLabels()}
	h, d, tokens := prefetchHost(t, w)
	if _, err := h.Start(context.Background(), prefetchManifest(t, h, nil)); err != nil {
		t.Fatal(err)
	}
	if slices.ContainsFunc(tokens.calls, func(c string) bool { return strings.HasPrefix(c, "issue") || strings.HasPrefix(c, "ready") }) {
		t.Fatalf("token calls %q", tokens.calls)
	}
	for _, c := range d.calls {
		if c[0] == "exec" && strings.Contains(strings.Join(c, " "), "/run/ensemble/secrets/github") {
			t.Fatalf("github write %q", c)
		}
	}
}

// A prefetch with a repo grant prefetches, then issues the grant for that repo alone; the run's mounts are those of
// the same start without github.
func TestPrefetchWithRepoGrant(t *testing.T) {
	w := &world{labels: goodLabels()}
	h, d, tokens := prefetchHost(t, w)
	write := map[string]string{"contents": "write"}
	path := prefetchManifest(t, h, func(f map[string]any) {
		f["github"] = map[string]any{"app": "ensemble-broker-bot", "permissions": write, "repo": "acme/out"}
	})
	if _, err := h.Start(context.Background(), path); err != nil {
		t.Fatal(err)
	}
	prefetch := slices.Index(tokens.calls, "prefetch acme/app main "+home+"/src/project "+container)
	issue := slices.Index(tokens.calls, "issue ensemble-broker-bot "+container)
	if prefetch < 0 || issue < prefetch || !slices.Equal(tokens.repos, []string{"acme/out"}) ||
		len(tokens.permissions) != 1 || !maps.Equal(tokens.permissions[0], write) {
		t.Fatalf("token calls %q repos %q permissions %v", tokens.calls, tokens.repos, tokens.permissions)
	}
	w2 := &world{labels: goodLabels()}
	h2, d2, _ := prefetchHost(t, w2)
	if _, err := h2.Start(context.Background(), prefetchManifest(t, h2, nil)); err != nil {
		t.Fatal(err)
	}
	want := runMounts(d2)
	for i := range want {
		want[i] = strings.ReplaceAll(want[i], h2.Home, h.Home)
	}
	if got := runMounts(d); len(got) == 0 || !slices.Equal(got, want) {
		t.Fatalf("mounts %q, without github %q", got, want)
	}
}

// An environment container without prefetch keeps the host App's GitHub tokens, whatever its network and secrets.
func TestEnvironmentWithoutPrefetchKeepsGitHub(t *testing.T) {
	w := &world{labels: goodLabels()}
	h, d, tokens := prefetchHost(t, w)
	h.Env = &fakeEnv{}
	path := prefetchManifest(t, h, func(f map[string]any) {
		delete(f, "prefetch")
		f["gateway"] = "mini-op"
		withSecrets([]any{secret("ensemble-broker-env/dummy", "dummy.txt")})(f)
		f["github"] = map[string]any{"app": "ensemble-broker-bot", "permissions": fixtureSet}
	})
	if _, err := h.Start(context.Background(), path); err != nil {
		t.Fatal(err)
	}
	if !slices.Contains(tokens.calls, "issue ensemble-broker-bot "+container) || slices.ContainsFunc(tokens.calls, func(c string) bool { return strings.HasPrefix(c, "prefetch") }) {
		t.Fatalf("token calls %q", tokens.calls)
	}
	if run, _ := helperRun(d); run != nil {
		t.Fatalf("helper ran: %q", run)
	}
	commands := startCommands(t, d)
	if len(commands) != 3 || commands[2].Argv[0] != "write" || string(commands[2].Stdin) != "ghs_token" {
		t.Fatalf("no GitHub token write in %q", d.verbs())
	}
}

func TestDefaultImageNewest(t *testing.T) {
	w := &world{labels: goodLabels()}
	h, _, _ := prefetchHost(t, w)
	older := strings.Repeat("c", 64)
	putFile(t, h.recordPath(older), `{"package":"/p","base_image":""}`)
	past := time.Now().Add(-time.Hour)
	if err := os.Chtimes(h.recordPath(older), past, past); err != nil {
		t.Fatal(err)
	}
	// A newer variant on a base image never counts.
	putFile(t, filepath.Join(h.Home, ".ensemble/docker/images", strings.Repeat("e", 64)+".json"), `{"package":"/p","base_image":"x:y"}`)
	h.Docker = &fakeDocker{respond: func(args []string, stdin []byte) ([]byte, error) {
		ref := args[len(args)-1]
		key := strings.TrimPrefix(ref, "ensemble-worker:")
		labels := goodLabels()
		labels["ensemble.key"] = key
		return inspectJSON("sha256:"+strings.Repeat(key[:1], 64), labels, time.Time{}), nil
	}}
	info, err := h.defaultImage(context.Background())
	if err != nil || info.ID != "sha256:"+strings.Repeat("a", 64) {
		t.Fatalf("%+v %v", info.ID, err)
	}
}
