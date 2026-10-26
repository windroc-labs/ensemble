package launcher

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"syscall"
	"testing"
	"time"

	"ensemble/broker/internal/codex"
	"ensemble/broker/internal/docker"
)

// deadlineReleases is fakeReleases that also records each lookup's deadline.
type deadlineReleases struct {
	fakeReleases
	deadlines []time.Time
}

func (d *deadlineReleases) Latest(ctx context.Context, pkg string) (string, error) {
	deadline, _ := ctx.Deadline()
	d.deadlines = append(d.deadlines, deadline)
	return d.fakeReleases.Latest(ctx, pkg)
}

func TestLatestClients(t *testing.T) {
	r := &deadlineReleases{fakeReleases: *newReleases()}
	got, err := Host{Releases: r}.latestClients(context.Background())
	end := time.Now()
	if err != nil || got != (ClientVersions{Claude: "2.1.7", Codex: "0.170.0", Opencode: "1.20.1"}) {
		t.Fatalf("%+v %v", got, err)
	}
	for _, deadline := range r.deadlines {
		if deadline.IsZero() || deadline.After(end.Add(2*time.Minute)) {
			t.Fatalf("lookup deadline %v", deadline)
		}
	}
	at := func(pkg, version string) map[string]string {
		latest := newReleases().latest
		latest[pkg] = version
		return latest
	}
	for name, tc := range map[string]struct {
		latest map[string]string
		err    error
		want   string
	}{
		"claude below its minimum":   {latest: at("@anthropic-ai/claude-code", "2.0.99"), want: "@anthropic-ai/claude-code"},
		"codex below its minimum":    {latest: at("@openai/codex", "0.162.9"), want: "@openai/codex"},
		"opencode below its minimum": {latest: at("opencode-ai", "1.19.2"), want: "opencode-ai"},
		"a prerelease":               {latest: at("opencode-ai", "1.20.1-beta.1"), want: "opencode-ai"},
		"opencode 2.x":               {latest: at("opencode-ai", "2.0.0"), want: "latest opencode-ai release 2.0.0 is not OpenCode 1.x"},
		"not a version":              {latest: at("@openai/codex", "latest"), want: "@openai/codex"},
		"a lookup failure":           {err: errors.New("@anthropic-ai/claude-code registry unavailable"), want: "@anthropic-ai/claude-code registry unavailable"},
	} {
		t.Run(name, func(t *testing.T) {
			_, err := Host{Releases: &fakeReleases{latest: tc.latest, err: tc.err}}.latestClients(context.Background())
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("%v", err)
			}
		})
	}
	minimums := map[string]string{"@anthropic-ai/claude-code": "2.1.0", "@openai/codex": codex.MinVersion, "opencode-ai": opencodeMinVersion}
	if _, err := (Host{Releases: &fakeReleases{latest: minimums}}).latestClients(context.Background()); err != nil {
		t.Fatalf("the minimums refused: %v", err)
	}
	if _, err := (Host{}).latestClients(context.Background()); err == nil {
		t.Fatal("no Releases looked up nothing and passed")
	}
}

func TestImageClients(t *testing.T) {
	labels := map[string]string{"ensemble.claude-version": "2.1.7", "ensemble.codex-version": "0.170.0", "ensemble.opencode-version": "1.20.1", "ensemble.key": "k"}
	got, ok := imageClients(labels)
	if want := (ClientVersions{Claude: "2.1.7", Codex: "0.170.0", Opencode: "1.20.1"}); !ok || got != want {
		t.Fatalf("%+v %v", got, ok)
	}
	for label := range clientArgs {
		for _, bad := range []string{"", "1.2", "v1.2.3", "1.2.3-rc.1", "01.2.3"} {
			changed := map[string]string{}
			for k, v := range labels {
				changed[k] = v
			}
			changed[clientArgs[label]] = bad
			if _, ok := imageClients(changed); ok {
				t.Fatalf("%s=%q accepted", clientArgs[label], bad)
			}
		}
	}
	if _, ok := imageClients(nil); ok {
		t.Fatal("no labels accepted")
	}
}

func TestClientVersionsDiffer(t *testing.T) {
	v := ClientVersions{Claude: "2.1.7", Codex: "0.170.0", Opencode: "1.20.1"}
	if v.differs(v) {
		t.Fatal("equal versions differ")
	}
	for _, other := range []ClientVersions{
		{Claude: "2.1.8", Codex: "0.170.0", Opencode: "1.20.1"},
		{Claude: "2.1.7", Codex: "0.169.0", Opencode: "1.20.1"},
		{Claude: "2.1.7", Codex: "0.170.0", Opencode: "1.20.2"},
	} {
		if !v.differs(other) {
			t.Fatalf("%+v does not differ from %+v", other, v)
		}
	}
}

// built is e's image of spec, at newReleases' versions, each later image build getting its own ID.
func (e *buildEnv) builtImage(t *testing.T, pkg string, spec BuildSpec) BuildResult {
	t.Helper()
	if e.ids == nil {
		e.ids = map[string]string{}
	}
	result, err := e.host().Build(context.Background(), pkg, spec, -1)
	if err != nil || !result.Rebuilt {
		t.Fatalf("%+v %v", result, err)
	}
	return result
}

// latestVersions is e's Latest as ClientVersions.
func (e *buildEnv) latestVersions() ClientVersions {
	l := e.releases.latest
	return ClientVersions{Claude: l["@anthropic-ai/claude-code"], Codex: l["@openai/codex"], Opencode: l["opencode-ai"]}
}

// checkState is the client check state, failing unless it is a 0600 file.
func (e *buildEnv) checkState(t *testing.T) (time.Time, string) {
	t.Helper()
	var state struct {
		CheckedAt time.Time `json:"checked_at"`
		Failure   *string   `json:"failure"`
	}
	if err := json.Unmarshal([]byte(private(t, filepath.Join(e.home, ".ensemble/docker/clients-check.json"))), &state); err != nil || state.Failure == nil {
		t.Fatalf("%v", err)
	}
	return state.CheckedAt, *state.Failure
}

// prunedLast fails unless calls end with a prune's listings.
func prunedLast(t *testing.T, e *buildEnv) {
	t.Helper()
	verbs := e.fake.verbs()
	if len(verbs) < 3 || !slices.Equal(verbs[len(verbs)-3:], []string{"container ls", "image ls", "image ls"}) {
		t.Fatalf("no prune at the end: %q", verbs)
	}
}

func layerBuilds(calls [][]string) int {
	n := 0
	for _, c := range calls {
		if c[0] == "build" && slices.Contains(c, "--build-arg") {
			n++
		}
	}
	return n
}

func TestCheckClientsRebuildsDifferingVariant(t *testing.T) {
	ctx := context.Background()
	for name, tc := range map[string]struct{ pkg, version string }{
		"claude newer":        {"@anthropic-ai/claude-code", "2.1.8"},
		"codex newer":         {"@openai/codex", "0.171.0"},
		"opencode newer":      {"opencode-ai", "1.20.2"},
		"a rolled-back Codex": {"@openai/codex", "0.169.0"},
	} {
		t.Run(name, func(t *testing.T) {
			e := newBuildEnv(t)
			first := e.builtImage(t, e.pkg, BuildSpec{})
			e.releases.latest[tc.pkg] = tc.version
			start := time.Now()
			got, err := e.host().CheckClients(ctx)
			if err != nil || got.Latest != e.latestVersions() || !slices.Equal(got.Rebuilt, []string{first.Image}) {
				t.Fatalf("%+v %v", got, err)
			}
			if clients, ok := imageClients(e.built[first.Image]); !ok || clients != e.latestVersions() {
				t.Fatalf("%+v", e.built[first.Image])
			}
			prunedLast(t, e)
			if at, failure := e.checkState(t); at.Before(start.Add(-time.Second)) || failure != "" {
				t.Fatalf("%v %q", at, failure)
			}
			again, err := e.host().Build(ctx, e.pkg, BuildSpec{}, -1)
			if err != nil || again.Rebuilt || again.Image != first.Image || again.Digest == first.Digest {
				t.Fatalf("%+v %v", again, err)
			}
		})
	}
	t.Run("a check rebuild keeps the variant's last use", func(t *testing.T) {
		e := newBuildEnv(t)
		first := e.builtImage(t, e.pkg, BuildSpec{})
		record := e.leasePath(strings.TrimPrefix(first.Image, "ensemble-worker:")) + ".json"
		used := time.Now().Add(-variantLease + time.Hour)
		if err := os.Chtimes(record, used, used); err != nil {
			t.Fatal(err)
		}
		e.releases.latest["@openai/codex"] = "0.171.0"
		if got, err := e.host().CheckClients(ctx); err != nil || !slices.Equal(got.Rebuilt, []string{first.Image}) {
			t.Fatalf("%+v %v", got, err)
		}
		if info, err := os.Stat(record); err != nil || !info.ModTime().Equal(used) {
			t.Fatalf("the check moved the last use: %v %v", info.ModTime(), err)
		}
	})
	t.Run("a base image and in-image plugins", func(t *testing.T) {
		e := newBaseEnv(t)
		spec := BuildSpec{BaseImage: baseRef, Plugins: []string{"x@owner/repo#" + strings.Repeat("a", 40)}, ImagePlugins: []string{"beta", "alpha"}}
		first := e.builtImage(t, e.pkg, spec)
		e.releases.latest["opencode-ai"] = "1.20.2"
		calls := len(e.fake.calls)
		got, err := e.host().CheckClients(ctx)
		if err != nil || !slices.Equal(got.Rebuilt, []string{first.Image}) {
			t.Fatalf("%+v %v", got, err)
		}
		check := e.fake.calls[calls:]
		key := strings.TrimPrefix(first.Image, "ensemble-worker:")
		if !slices.ContainsFunc(check, func(c []string) bool { return slices.Equal(c, []string{"tag", baseID, "ensemble-user:" + key}) }) ||
			!slices.ContainsFunc(check, func(c []string) bool {
				return slices.Equal(c[len(c)-2:], []string{"alpha", "beta"}) && slices.Contains(c, "node")
			}) {
			t.Fatalf("%q", check)
		}
		layer := packageBuild(check)
		for _, arg := range []string{"ENSEMBLE_PLUGIN_DIRS=/opt/ensemble/plugins/x:/opt/windroc/plugins/alpha:/opt/windroc/plugins/beta", "OPENCODE_VERSION=1.20.2"} {
			if !slices.Contains(layer, arg) {
				t.Fatalf("%s absent: %q", arg, layer)
			}
		}
		noPull(t, check)
	})
}

func TestCheckClientsCurrentRebuildsNothing(t *testing.T) {
	e := newBuildEnv(t)
	e.builtImage(t, e.pkg, BuildSpec{})
	e.writeCheckState(t, time.Now().Add(-2*time.Hour), "ensemble-worker:x: Building the ensemble image stage failed.")
	calls := len(e.fake.calls)
	got, err := e.host().CheckClients(context.Background())
	if err != nil || got.Latest != e.latestVersions() || got.Rebuilt == nil || len(got.Rebuilt) != 0 {
		t.Fatalf("%+v %v", got, err)
	}
	if layerBuilds(e.fake.calls[calls:]) != 0 {
		t.Fatalf("rebuilt: %q", e.fake.verbs()[calls:])
	}
	prunedLast(t, e)
	if at, failure := e.checkState(t); time.Since(at) > time.Minute || failure != "" {
		t.Fatalf("%v %q", at, failure)
	}
}

func TestCheckResultJSON(t *testing.T) {
	data, err := json.Marshal(CheckResult{Latest: ClientVersions{Claude: "2.1.7", Codex: "0.170.0", Opencode: "1.20.1"}, Rebuilt: []string{}})
	if err != nil || string(data) != `{"latest":{"claude":"2.1.7","codex":"0.170.0","opencode":"1.20.1"},"rebuilt":[]}` {
		t.Fatalf("%s %v", data, err)
	}
}

func TestCheckClientsRegistryDown(t *testing.T) {
	for name, tc := range map[string]struct {
		mutate  func(*fakeReleases)
		failure string
	}{
		"a lookup failure": {func(r *fakeReleases) { r.err = errors.New("@openai/codex registry unavailable") }, "registry: @openai/codex registry unavailable"},
		"below its minimum": {func(r *fakeReleases) { r.latest["@anthropic-ai/claude-code"] = "2.0.9" },
			"registry: latest @anthropic-ai/claude-code release 2.0.9 is below 2.1.0"},
	} {
		t.Run(name, func(t *testing.T) {
			e := newBuildEnv(t)
			e.builtImage(t, e.pkg, BuildSpec{})
			tc.mutate(e.releases)
			calls := len(e.fake.calls)
			_, err := e.host().CheckClients(context.Background())
			if message, cause := buildFailure(t, err); message != "Cannot look up the latest client releases." || cause != "" {
				t.Fatalf("%q %q", message, cause)
			}
			if layerBuilds(e.fake.calls[calls:]) != 0 {
				t.Fatalf("rebuilt: %q", e.fake.verbs()[calls:])
			}
			prunedLast(t, e)
			if _, failure := e.checkState(t); failure != tc.failure {
				t.Fatalf("%q", failure)
			}
		})
	}
}

func TestCheckClientsRebuildFailureKeepsImage(t *testing.T) {
	e := newBuildEnv(t)
	images := []string{e.builtImage(t, e.pkg, BuildSpec{}).Image, e.builtImage(t, otherPackage(t), BuildSpec{}).Image}
	slices.Sort(images)
	failing, next := images[0], images[1]
	before := e.built[failing]
	inner := e.fake.respond
	e.fake.respond = func(args []string) ([]byte, error) {
		if args[0] == "build" && args[slices.Index(args, "--tag")+1] == failing {
			return nil, &docker.CommandError{Exit: 1, Stderr: "step 3 failed\nRUN false"}
		}
		return inner(args)
	}
	e.releases.latest["@openai/codex"] = "0.171.0"
	got, err := e.host().CheckClients(context.Background())
	want := failing + ": Building the ensemble image stage failed."
	if message, cause := buildFailure(t, err); message != want || cause != "" {
		t.Fatalf("%q %q", message, cause)
	}
	if !slices.Equal(got.Rebuilt, []string{next}) {
		t.Fatalf("%+v", got)
	}
	if e.built[failing]["ensemble.codex-version"] != "0.170.0" || !reflect.DeepEqual(e.built[failing], before) {
		t.Fatalf("the failing variant's image changed: %v", e.built[failing])
	}
	if e.built[next]["ensemble.codex-version"] != "0.171.0" {
		t.Fatalf("the next variant not rebuilt: %v", e.built[next])
	}
	prunedLast(t, e)
	if _, failure := e.checkState(t); failure != want {
		t.Fatalf("%q", failure)
	}
}

func TestCheckClientsSingleFlight(t *testing.T) {
	e := newBuildEnv(t)
	e.builtImage(t, e.pkg, BuildSpec{})
	path := filepath.Join(e.home, ".ensemble/docker/clients-check.lock")
	f, err := os.OpenFile(path, os.O_RDWR|os.O_CREATE, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		t.Fatal(err)
	}
	e.releases.latest["@openai/codex"] = "0.171.0"
	e.releases.asked = nil
	calls := len(e.fake.calls)
	got, err := e.host().CheckClients(context.Background())
	data, _ := json.Marshal(got)
	if err != nil || string(data) != `{"rebuilt":[]}` || len(e.fake.calls) != calls || e.releases.asked != nil {
		t.Fatalf("%s %v %q", data, err, e.fake.verbs()[calls:])
	}
	if _, err := os.Stat(filepath.Join(e.home, ".ensemble/docker/clients-check.json")); !os.IsNotExist(err) {
		t.Fatalf("state written: %v", err)
	}
}

func TestCheckClientsDropsStaleRecords(t *testing.T) {
	ctx := context.Background()
	record := func(e *buildEnv, key string) string { return e.leasePath(key) + ".json" }
	for name, tc := range map[string]struct {
		setup func(t *testing.T, e *buildEnv) string // returns the record's key
		kept  bool
	}{
		"unreadable": {setup: func(t *testing.T, e *buildEnv) string {
			key := strings.Repeat("1", 64)
			putFile(t, e.leasePath(key), "")
			putFile(t, record(e, key), "{")
			return key
		}},
		"image absent": {setup: func(t *testing.T, e *buildEnv) string {
			key := strings.Repeat("2", 64)
			putFile(t, e.leasePath(key), "")
			putFile(t, record(e, key), `{"package":"`+e.pkg+`","base_image":"","plugins":[],"image_plugins":[]}`)
			return key
		}},
		"prepare fails": {setup: func(t *testing.T, e *buildEnv) string {
			key := strings.TrimPrefix(e.builtImage(t, e.pkg, BuildSpec{}).Image, "ensemble-worker:")
			if err := os.RemoveAll(filepath.Join(e.pkg, "src/ensemble/session")); err != nil {
				t.Fatal(err)
			}
			return key
		}},
		"another key": {setup: func(t *testing.T, e *buildEnv) string {
			key := strings.TrimPrefix(e.builtImage(t, e.pkg, BuildSpec{}).Image, "ensemble-worker:")
			putFile(t, filepath.Join(e.pkg, "src/ensemble/session/sample.py"), "changed")
			return key
		}},
		"unused": {kept: true, setup: func(t *testing.T, e *buildEnv) string {
			key := strings.TrimPrefix(e.builtImage(t, e.pkg, BuildSpec{}).Image, "ensemble-worker:")
			stale := time.Now().Add(-variantLease - time.Minute)
			if err := os.Chtimes(record(e, key), stale, stale); err != nil {
				t.Fatal(err)
			}
			return key
		}},
	} {
		t.Run(name, func(t *testing.T) {
			e := newBuildEnv(t)
			key := tc.setup(t, e)
			e.releases.latest["@openai/codex"] = "0.171.0"
			calls := len(e.fake.calls)
			got, err := e.host().CheckClients(ctx)
			if err != nil || len(got.Rebuilt) != 0 || layerBuilds(e.fake.calls[calls:]) != 0 {
				t.Fatalf("%+v %v %q", got, err, e.fake.verbs()[calls:])
			}
			if _, err := os.Stat(record(e, key)); os.IsNotExist(err) == tc.kept {
				t.Fatalf("record kept=%v: %v", !os.IsNotExist(err), err)
			}
		})
	}
}
