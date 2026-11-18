package launcher

import (
	"context"
	"errors"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/protocol"
)

// failingRm fails d's removal of every container whose name starts with prefix.
func failingRm(d *fakeDocker, prefix string) {
	base := d.respond
	d.respond = func(args []string, stdin []byte) ([]byte, error) {
		if len(args) == 3 && args[0] == "rm" && args[1] == "--force" && strings.HasPrefix(args[2], prefix) {
			return nil, errors.New("rm failed")
		}
		return base(args, stdin)
	}
}

func TestWorkspaceCheckNamesAHelperItCannotRemove(t *testing.T) {
	c := clean()
	c.runErr = &docker.CommandError{Exit: 1, Stderr: "boom"}
	d := c.docker()
	failingRm(d, "ensemble-check-")
	_, err := Host{}.WorkspaceRm(context.Background(), Daemon{Runner: d}, volume, false)
	if e := protoErr(t, err); e.Code != protocol.WorkspaceFailed || !strings.Contains(e.Message, " Remove its helper with docker rm --force ensemble-check-") {
		t.Fatalf("%+v", e)
	}
}

func TestPrefetchNamesAHelperItCannotRemove(t *testing.T) {
	w := &world{labels: goodLabels(), prefetchErr: &docker.CommandError{Exit: 128, Stderr: "boom"}}
	h, d, _ := prefetchHost(t, w)
	failingRm(d, "ensemble-prefetch-")
	_, err := h.Start(context.Background(), prefetchManifest(t, h, nil))
	run, _ := helperRun(d)
	if e := protoErr(t, err); e.Code != protocol.StartFailed || !strings.Contains(e.Message, " Remove its helper with docker rm --force "+flag(run, "--name")+".") {
		t.Fatalf("%+v", e)
	}
}

func TestPruneNotesARecordItCannotRemove(t *testing.T) {
	key := strings.Repeat("7", 64)
	e := newBuildEnv(t)
	e.images = "ensemble-worker:" + key + " sha256:" + strings.Repeat("8", 64) + "\n"
	h := e.host()
	h.Now = func() time.Time { return time.Date(2100, 1, 1, 0, 0, 0, 0, time.UTC) }
	lease := e.records().leasePath(key)
	putFile(t, filepath.Join(lease, "kept"), "")
	h.prune(context.Background(), "")
	if got := e.progress.String(); !strings.Contains(got, "removed ensemble-worker:"+key) || !strings.Contains(got, "cannot remove "+lease) {
		t.Fatalf("%q", got)
	}
}
