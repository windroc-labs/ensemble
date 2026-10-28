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

// defaultImage is the newest default worker image: a broker-built image whose variant record names no base image,
// newest by the record's mtime (a build's last use). With none, it builds one from run's package, as build does.
func (h Host) defaultImage(ctx context.Context, run string) (imageInfo, error) {
	type candidate struct {
		key string
		at  time.Time
	}
	var found []candidate
	entries, _ := os.ReadDir(h.dockerPath("images"))
	for _, entry := range entries {
		m := variantFile.FindStringSubmatch(entry.Name())
		if m == nil || m[2] != "json" {
			continue
		}
		var v variant
		data, err := os.ReadFile(h.recordPath(m[1]))
		info, statErr := entry.Info()
		if err != nil || statErr != nil || json.Unmarshal(data, &v) != nil || v.BaseImage != "" {
			continue
		}
		found = append(found, candidate{m[1], info.ModTime()})
	}
	slices.SortFunc(found, func(a, b candidate) int { return b.at.Compare(a.at) })
	for _, c := range found {
		info, err := h.checkImage(ctx, "ensemble-worker:"+c.key)
		var e *protocol.Error
		if errors.As(err, &e) && e.Code == protocol.DockerUnreachable {
			return imageInfo{}, err
		}
		if err == nil {
			return info, nil
		}
	}
	var v variant
	if data, err := os.ReadFile(h.recordPath(strings.TrimPrefix(run, "ensemble-worker:"))); err != nil || json.Unmarshal(data, &v) != nil || !filepath.IsAbs(v.Package) {
		return imageInfo{}, protocol.New(protocol.StartFailed, "No default worker image to prefetch with, and no build record of "+run+" to build one from; run ensemble-broker build without a base image.")
	}
	built, err := h.Build(ctx, v.Package, BuildSpec{}, -1)
	var e *protocol.Error
	if errors.As(err, &e) && e.Code != protocol.DockerUnreachable {
		// start's protocol has no build_failed.
		return imageInfo{}, &protocol.Error{Code: protocol.StartFailed, Message: "No default worker image to prefetch with; building it failed: " + e.Message,
			Cause: e.Cause, Cleanup: protocol.NotNeeded}
	}
	if err != nil {
		return imageInfo{}, err
	}
	return h.checkImage(ctx, built.Image)
}

// prefetch clones m's prefetch repo into its new workspace volume through a throwaway helper: the default worker
// image on the egress proxy's workers network, whatever m's gateway, running as run's agent (the volume's owner). The
// token reaches it only on stdin; GitHub.Prefetch owns the token and the clone script.
func (h Host) prefetch(ctx context.Context, c string, m Manifest, run imageInfo, px egressProxy) error {
	network, err := h.caller(ctx, px, workersKey)
	if err != nil {
		return err
	}
	helper, err := h.defaultImage(ctx, m.Image)
	if err != nil {
		return err
	}
	owner := run.Config.Labels["ensemble.agent-uid"] + ":" + run.Config.Labels["ensemble.agent-gid"]
	name := "ensemble-prefetch-" + randomHex()
	start := func(argv []string, stdin []byte) error {
		cctx, cancel := context.WithTimeout(ctx, prefetchTimeout)
		defer cancel()
		args := append([]string{"run", "--rm", "--interactive", "--pull", "never", "--name", name, "--label", prefetchLabel + "=1",
			"--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", owner}, network.args()...)
		args = append(args, "--env", "HOME="+prefetchHome, "--env", "GIT_CONFIG_NOSYSTEM=1",
			"--tmpfs", prefetchHome+":rw,noexec,nosuid,nodev,size=65536,mode=0700,uid="+run.Config.Labels["ensemble.agent-uid"]+",gid="+run.Config.Labels["ensemble.agent-gid"],
			"--mount", volumeMount(m.Workspace, m.CWD, false), "--entrypoint", argv[0], helper.ID)
		_, err := h.Docker.Run(cctx, append(args, argv[1:]...), stdin)
		if err != nil {
			_, _ = h.Docker.Run(context.WithoutCancel(ctx), []string{"rm", "--force", name}, nil)
		}
		return err
	}
	return h.GitHub.Prefetch(ctx, m.Prefetch.Repo, m.Prefetch.Ref, m.CWD, c, start)
}
