package launcher

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"time"

	"ensemble/broker/internal/protocol"
)

const (
	prefetchTimeout = 10 * time.Minute
	prefetchLabel   = "ensemble.prefetch"
	prefetchHome    = "/run/prefetch" // the helper's HOME, a tmpfs holding only the token
	// nonceLabel marks a workspace volume a prefetching start created, so only that start prefetches into or removes it.
	nonceLabel = "ensemble.prefetch-start"
)

// defaultImage is the newest (by record mtime) broker-built image with no base image; with none, one built from run's package.
func (h Host) defaultImage(ctx context.Context, d Daemon, r records, run string) (imageInfo, error) {
	type candidate struct {
		key string
		at  time.Time
	}
	var found []candidate
	for _, record := range r.variants() {
		var v variant
		data, err := os.ReadFile(r.recordPath(record.key))
		info, statErr := record.entry.Info()
		if err != nil || statErr != nil || json.Unmarshal(data, &v) != nil || v.BaseImage != "" {
			continue
		}
		found = append(found, candidate{record.key, info.ModTime()})
	}
	slices.SortFunc(found, func(a, b candidate) int { return b.at.Compare(a.at) })
	for _, c := range found {
		info, err := h.checkImage(ctx, d, "ensemble-worker:"+c.key)
		var e *protocol.Error
		if errors.As(err, &e) && e.Code == protocol.DockerUnreachable {
			return imageInfo{}, err
		}
		if err == nil {
			return info, nil
		}
	}
	var v variant
	if data, err := os.ReadFile(r.recordPath(strings.TrimPrefix(run, "ensemble-worker:"))); err != nil || json.Unmarshal(data, &v) != nil || !filepath.IsAbs(v.Package) {
		return imageInfo{}, protocol.New(protocol.StartFailed, "No default worker image to prefetch with, and no build record of "+run+" to build one from; run ensemble-broker build without a base image.")
	}
	built, err := h.build(ctx, d, r, v.Package, BuildSpec{}, -1)
	var e *protocol.Error
	if errors.As(err, &e) && e.Code != protocol.DockerUnreachable {
		// start's protocol has no build_failed.
		return imageInfo{}, &protocol.Error{Code: protocol.StartFailed, Message: "No default worker image to prefetch with; building it failed: " + e.Message,
			Cause: e.Cause, Cleanup: protocol.NotNeeded}
	}
	if err != nil {
		return imageInfo{}, err
	}
	return h.checkImage(ctx, d, built.Image)
}

// prefetch clones m's repo into its new workspace through a throwaway default-image helper on the workers network, as
// run's agent; the token reaches it only on stdin.
func (h Host) prefetch(ctx context.Context, d Daemon, r records, c string, m Manifest, run imageInfo, px egressProxy) error {
	network, err := h.caller(ctx, d, px, workersKey)
	if err != nil {
		return err
	}
	helper, err := h.defaultImage(ctx, d, r, m.Image)
	if err != nil {
		return err
	}
	owner := run.Config.Labels["ensemble.agent-uid"] + ":" + run.Config.Labels["ensemble.agent-gid"]
	name := "ensemble-prefetch-" + randomHex()
	stranded := ""
	start := func(argv []string, stdin []byte) error {
		cctx, cancel := context.WithTimeout(ctx, prefetchTimeout)
		defer cancel()
		args := append([]string{"run", "--rm", "--interactive", "--pull", "never", "--name", name, "--label", prefetchLabel + "=1",
			"--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", owner}, network.args()...)
		args = append(args, "--env", "HOME="+prefetchHome, "--env", "GIT_CONFIG_NOSYSTEM=1",
			"--tmpfs", prefetchHome+":rw,noexec,nosuid,nodev,size=65536,mode=0700,uid="+run.Config.Labels["ensemble.agent-uid"]+",gid="+run.Config.Labels["ensemble.agent-gid"],
			"--mount", volumeMount(m.Workspace, m.CWD, false), "--entrypoint", argv[0], helper.ID)
		_, err := d.Runner.Run(cctx, append(args, argv[1:]...), stdin)
		if err != nil {
			stranded = removeHelper(ctx, d, name)
		}
		return err
	}
	err = h.GitHub.Prefetch(ctx, m.Prefetch.Repo, m.Prefetch.Ref, m.CWD, c, start)
	if e, ok := errors.AsType[*protocol.Error](err); ok {
		e.Message += stranded
	}
	return err
}

// removeHelper force-removes a helper container whose run failed, whatever ctx's state; hint is what its failure adds
// to the run's.
func removeHelper(ctx context.Context, d Daemon, name string) (hint string) {
	if _, err := d.Runner.Run(context.WithoutCancel(ctx), []string{"rm", "--force", name}, nil); err != nil {
		return " Remove its helper with docker rm --force " + name + "."
	}
	return ""
}
