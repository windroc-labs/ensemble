package launcher

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"path/filepath"
	"strings"
	"time"

	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/protocol"
)

const checkTimeout = 120 * time.Second

// WorkspaceRm deletes a workspace volume once an offline check of its repos finds no unsaved work, or at once with force;
// an absent volume is success. A session's workspace (ensemble-ws-<name>) takes its state volume with it, first, so a
// failed state removal keeps the workspace that reclaim finds the session by.
func (h Host) WorkspaceRm(ctx context.Context, d Daemon, volume string, force bool) (WorkspaceResult, error) {
	session, isSession := strings.CutPrefix(volume, "ensemble-ws-")
	removeState := func() error {
		if !isSession {
			return nil
		}
		return h.removeStateVolume(ctx, d, session)
	}
	labels, err := h.volumeLabels(ctx, d, volume)
	if docker.Absent(err) {
		if err := removeState(); err != nil {
			return WorkspaceResult{}, err
		}
		return WorkspaceResult{Volume: volume}, nil
	}
	if err != nil {
		return WorkspaceResult{}, failure(err, protocol.WorkspaceFailed, "Cannot inspect volume "+volume+".")
	}
	if labels[kindLabel] == "" {
		return WorkspaceResult{}, protocol.Newf(protocol.WorkspaceRejected, "Volume %s is not an ensemble workspace (no %s label).", volume, kindLabel)
	}
	if !force {
		dirty, err := h.checkWorkspace(ctx, d, volume, labels[workspaceLabel])
		if err != nil {
			return WorkspaceResult{}, err
		}
		if len(dirty) > 0 {
			return WorkspaceResult{}, protocol.Newf(protocol.WorkspaceDirty,
				"Workspace %s has uncommitted or unpushed work in: %s. Delete it anyway with ensemble-broker workspace-rm --volume %s --force.",
				volume, strings.Join(dirty, ", "), volume)
		}
	}
	if err := removeState(); err != nil {
		return WorkspaceResult{}, err
	}
	if _, err := d.Runner.Run(ctx, []string{"volume", "rm", volume}, nil); err != nil {
		return WorkspaceResult{}, diagnosed(err, protocol.WorkspaceFailed, "Deleting volume "+volume+" failed.")
	}
	return WorkspaceResult{Volume: volume, Removed: true}, nil
}

// checkWorkspace runs the newest worker image's workspace-check on the volume, read-only and offline, mounted where the worker saw it.
func (h Host) checkWorkspace(ctx context.Context, d Daemon, volume, path string) ([]string, error) {
	if !strings.HasPrefix(path, "/") || filepath.Clean(path) != path || hasControl(path) {
		return nil, protocol.Newf(protocol.WorkspaceRejected, "Volume %s has no clean absolute %s path.", volume, workspaceLabel)
	}
	if err := checkTree(path, protocol.WorkspaceRejected); err != nil {
		return nil, err
	}
	out, err := d.Runner.Run(ctx, []string{"image", "ls", "--filter", "reference=ensemble-worker", "--format", "{{.Repository}}:{{.Tag}}"}, nil)
	if err != nil {
		return nil, failure(err, protocol.WorkspaceFailed, "Cannot list worker images.")
	}
	image := ""
	for _, ref := range strings.Fields(string(out)) {
		if imagePattern.MatchString(ref) {
			image = ref
			break
		}
	}
	if image == "" {
		return nil, protocol.New(protocol.WorkspaceFailed, "No worker image to check the workspace with; run ensemble-broker build.")
	}
	info, err := h.checkImage(ctx, d, image)
	if err != nil {
		return nil, failure(err, protocol.WorkspaceFailed, "Worker image "+image+" cannot check the workspace.")
	}
	id := make([]byte, 16)
	_, _ = rand.Read(id)
	name := "ensemble-check-" + hex.EncodeToString(id)
	cctx, cancel := context.WithTimeout(ctx, checkTimeout)
	defer cancel()
	out, err = d.Runner.Run(cctx, []string{"run", "--rm", "--name", name, "--label", "ensemble.check=1", "--cap-drop", "ALL",
		"--security-opt", "no-new-privileges", "--user", "agent", "--network", "none", "--mount", volumeMount(volume, path, true),
		"--entrypoint", Python, info.ID, "-I", checker, path, cache}, nil)
	if err != nil {
		_, _ = d.Runner.Run(context.WithoutCancel(ctx), []string{"rm", "--force", name}, nil)
		return nil, diagnosed(err, protocol.WorkspaceFailed, "The workspace check of "+volume+" failed.")
	}
	var found struct{ Dirty *[]string }
	if json.Unmarshal(out, &found) != nil || found.Dirty == nil {
		return nil, protocol.New(protocol.WorkspaceFailed, "The workspace check of "+volume+" printed no result.")
	}
	return *found.Dirty, nil
}
