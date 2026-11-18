package launcher

import (
	"context"
	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/protocol"
	"slices"
	"strings"
)

const (
	stateNew  = "new"
	stateKeep = "keep"
)

func (h Host) ensureStateVolume(ctx context.Context, d Daemon, m Manifest, info imageInfo) (created bool, _ error) {
	v := volumesOf(m.Name).State
	if m.State == stateNew {
		if err := h.removeStateVolume(ctx, d, m.Name, protocol.StartFailed); err != nil {
			return false, err
		}
	}
	labels, err := h.volumeLabels(ctx, d, v)
	created = docker.Absent(err)
	l := info.Config.Labels
	owner := l["ensemble.agent-uid"] + ":" + l["ensemble.agent-gid"]
	if created {
		create := []string{"volume", "create", "--label", stateLabel + "=" + m.Name, "--label", stateOwnerLabel + "=" + owner, v}
		if _, err := d.Runner.Run(ctx, create, nil); err != nil {
			return false, failure(err, protocol.StartFailed, "Creating state volume "+v+" failed.")
		}
		labels, err = h.volumeLabels(ctx, d, v)
	}
	if err != nil {
		return h.undo(ctx, startVolume{d, v, created}, failure(err, protocol.StartFailed, "Cannot inspect state volume "+v+"."))
	}
	if labels[stateLabel] != m.Name {
		return false, protocol.Newf(protocol.MountRejected, "Volume %s is not the client state of %s (%s %q).", v, m.Name, stateLabel, labels[stateLabel])
	}
	if !created {
		return false, nil
	}
	helper := []string{"run", "--rm", "--log-driver", "none", "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges"}
	chown := append(slices.Clone(helper), "--cap-add", "CHOWN", "--user", "0:0", "--mount", volumeMount(v, "/state", false), "--entrypoint", "/bin/chown",
		info.ID, owner, "/state")
	if _, err := d.Runner.Run(ctx, chown, nil); err != nil {
		return h.undo(ctx, startVolume{d, v, created}, failure(err, protocol.StartFailed, "Giving state volume "+v+" to agent failed."))
	}
	mkdir := append(slices.Clone(helper), "--user", owner, "--mount", volumeMount(v, "/state", false), "--entrypoint", "/bin/mkdir",
		info.ID, "-p", "-m", "0700")
	for _, dir := range stateDirs(m) {
		mkdir = append(mkdir, "/state/"+dir)
	}
	if _, err := d.Runner.Run(ctx, mkdir, nil); err != nil {
		return h.undo(ctx, startVolume{d, v, created}, failure(err, protocol.StartFailed, "Making the client dirs in state volume "+v+" failed."))
	}
	return true, nil
}

// removeStateVolume removes the readers first: they mount the volume.
func (h Host) removeStateVolume(ctx context.Context, d Daemon, name string, code protocol.Code) error {
	v := volumesOf(name).State
	out, err := d.Runner.Run(ctx, []string{"ps", "--all", "--quiet", "--filter", "label=" + readerLabel + "=" + name}, nil)
	if err != nil {
		return failure(err, code, "Cannot list the readers of "+v+".")
	}
	if readers := strings.Fields(string(out)); len(readers) > 0 {
		if _, err := d.Runner.Run(ctx, append([]string{"rm", "--force"}, readers...), nil); err != nil {
			return failure(err, code, "Removing the readers of "+v+" failed.")
		}
	}
	if _, err := d.Runner.Run(ctx, []string{"volume", "rm", v}, nil); err != nil && !docker.Absent(err) {
		return diagnosed(err, code, "Deleting volume "+v+" failed.")
	}
	return nil
}
