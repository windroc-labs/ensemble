package launcher

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"ensemble/broker/internal/protocol"
)

const otherDaemon = "WXYZ:ABCD:EFGH:IJKL:MNOP:QRST:UVWX:YZ23:4567:ABCD:EFGH:IJKL"

func exists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}

func TestBuildRecordsUnderItsDaemon(t *testing.T) {
	e := newBuildEnv(t)
	got, err := e.lookupHost().Build(context.Background(), e.pkg, BuildSpec{Daemon: testDaemon}, -1)
	if err != nil {
		t.Fatal(err)
	}
	key := strings.TrimPrefix(got.Image, "ensemble-worker:")
	if !exists(filepath.Join(e.imagesDir(), key)) || !exists(filepath.Join(e.imagesDir(), key+".json")) {
		t.Fatalf("no lease or record under %s", e.imagesDir())
	}
	if exists(filepath.Join(e.home, ".ensemble/docker/images", key+".json")) {
		t.Fatal("a record outside any daemon's dir")
	}
	if e.fake.calls[0][0] != "info" {
		t.Fatalf("first call %q, want the daemon's ID first", e.fake.calls[0])
	}
}

func TestBuildOnAnotherDaemonRebuildsThere(t *testing.T) {
	e := newBuildEnv(t)
	first := e.builtImage(t, e.pkg, BuildSpec{Daemon: testDaemon})
	key := strings.TrimPrefix(first.Image, "ensemble-worker:")
	firstRecord := filepath.Join(e.imagesDir(), key+".json")
	e.daemon, e.built = otherDaemon, map[string]map[string]string{}
	second := e.builtImage(t, e.pkg, BuildSpec{Daemon: testDaemon})
	if second.Image != first.Image || !second.Rebuilt {
		t.Fatalf("%+v, want %s rebuilt on the other daemon", second, first.Image)
	}
	if !exists(filepath.Join(e.imagesDir(), key+".json")) || !exists(firstRecord) {
		t.Fatal("want a record under each daemon")
	}
}

func TestCheckClientsLeavesAnotherDaemonsRecords(t *testing.T) {
	e := newBuildEnv(t)
	got := e.builtImage(t, e.pkg, BuildSpec{Daemon: testDaemon})
	record := filepath.Join(e.imagesDir(), strings.TrimPrefix(got.Image, "ensemble-worker:")+".json")
	e.daemon, e.built = otherDaemon, map[string]map[string]string{}
	result, err := e.lookupHost().CheckClients(context.Background())
	if err != nil || len(result.Rebuilt) != 0 {
		t.Fatalf("%+v %v", result, err)
	}
	if !exists(record) {
		t.Fatal("check-clients on another daemon dropped this daemon's record")
	}
	if !exists(filepath.Join(e.imagesDir(), "clients-check.json")) {
		t.Fatal("no check state under the checked daemon")
	}
}

func TestPruneKeepsAnotherDaemonsRecords(t *testing.T) {
	e := newBuildEnv(t)
	key := strings.Repeat("b", 64)
	lease := filepath.Join(e.imagesDir(), key)
	putFile(t, lease, "")
	putFile(t, lease+".json", `{"package":"/p"}`)
	e.daemon = otherDaemon
	e.images = "ensemble-worker:" + key + " sha256:" + strings.Repeat("c", 64)
	h := e.lookupHost()
	d, r, err := h.identify(context.Background(), h.Daemons[0], protocol.BuildFailed)
	if err != nil {
		t.Fatal(err)
	}
	h.prune(context.Background(), d, r, "")
	if removed := e.fake.removed(); len(removed) != 1 || removed[0] != "ensemble-worker:"+key {
		t.Fatalf("removed %q, want the other daemon's unleased image", removed)
	}
	if !exists(lease) || !exists(lease+".json") {
		t.Fatal("pruning another daemon removed this daemon's lease or record")
	}
}

func TestBuildDaemonIDFailure(t *testing.T) {
	for _, tc := range []struct {
		name, info string
		err        error
		code       protocol.Code
	}{
		{"unreachable", "", unreachableErr, protocol.DockerUnreachable},
		{"path", `"../x"` + "\n", nil, protocol.BuildFailed},
		{"not json", "abc\n", nil, protocol.BuildFailed},
		{"empty", `""` + "\n", nil, protocol.BuildFailed},
	} {
		t.Run(tc.name, func(t *testing.T) {
			e := newBuildEnv(t)
			e.info, e.infoErr = tc.info, tc.err
			if tc.info == "" && tc.err == nil {
				e.info = " "
			}
			_, err := e.lookupHost().Build(context.Background(), e.pkg, BuildSpec{Daemon: testDaemon}, -1)
			var pe *protocol.Error
			if !errors.As(err, &pe) || pe.Code != tc.code {
				t.Fatalf("%v, want %s", err, tc.code)
			}
			if verbs := e.fake.verbs(); len(verbs) != 1 {
				t.Fatalf("calls %q, want only the ID lookup", verbs)
			}
			if exists(filepath.Join(e.home, ".ensemble/docker/images")) {
				t.Fatal("records written without a daemon")
			}
			_, err = e.lookupHost().CheckClients(context.Background())
			if !errors.As(err, &pe) || pe.Code != tc.code {
				t.Fatalf("check-clients %v, want %s", err, tc.code)
			}
		})
	}
}

func TestDefaultImageIgnoresAnotherDaemon(t *testing.T) {
	w := &world{labels: goodLabels()}
	h, _, _ := prefetchHost(t, w)
	other := records{filepath.Join(h.Home, ".ensemble/docker/images", otherDaemon)}
	key := strings.Repeat("c", 64)
	putFile(t, other.recordPath(key), `{"package":"/p","base_image":""}`)
	future := time.Date(2100, 1, 1, 0, 0, 0, 0, time.UTC)
	if err := os.Chtimes(other.recordPath(key), future, future); err != nil {
		t.Fatal(err)
	}
	var inspected []string
	h = withRunner(h, &fakeDocker{respond: func(args []string, _ []byte) ([]byte, error) {
		ref := args[len(args)-1]
		inspected = append(inspected, ref)
		labels := goodLabels()
		labels["ensemble.key"] = strings.TrimPrefix(ref, "ensemble-worker:")
		return inspectJSON("sha256:"+strings.Repeat("a", 64), labels, time.Time{}), nil
	}})
	if _, err := h.defaultImage(context.Background(), h.Daemons[0], testRecords(h.Home), "ensemble-worker:"+imageKey); err != nil {
		t.Fatal(err)
	}
	for _, ref := range inspected {
		if strings.Contains(ref, key) {
			t.Fatalf("inspected %s, a variant recorded on another daemon", ref)
		}
	}
}

// A daemon whose check fails leaves every other daemon's check and prune; its error is the result's.
func TestCheckClientsIteratesDaemons(t *testing.T) {
	for _, localUp := range []bool{true, false} {
		e := newBuildEnv(t)
		first := e.builtImage(t, e.pkg, BuildSpec{Daemon: testDaemon})
		e.releases.latest["@openai/codex"] = "0.171.0"
		down := &fakeDocker{respond: func([]string, []byte) ([]byte, error) { return nil, unreachableErr }}
		h := onTwo(e.lookupHost(), e.fake, down)
		if localUp {
			h = onTwo(e.lookupHost(), down, e.fake)
		}
		from := len(e.fake.calls)
		got, err := h.CheckClients(context.Background())
		failed := protoErr(t, err)
		if failed.Code != "docker_unreachable" || !strings.Contains(failed.Message, remoteEndpoint) ||
			!slices.Equal(got.Rebuilt, []string{first.Image}) || len(down.calls) == 0 {
			t.Fatalf("local up %v: %+v %v; unreachable daemon calls %q", localUp, got, err, down.calls)
		}
		if !slices.Contains(e.fake.verbs()[from:], "image ls") {
			t.Fatalf("local up %v: no prune on the reachable daemon: %q", localUp, e.fake.verbs()[from:])
		}
	}
}
