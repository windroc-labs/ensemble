package launcher

import (
	"context"
	"crypto/rand"
	"encoding/csv"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"time"

	"ensemble/broker/internal/audit"
	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/protocol"
)

// The container's client homes and the state volumes' names and labels; the protocol's state-volume.json holds the
// same values.
const (
	ClientHomes       = "/home/agent/.ensemble-state"
	stateVolumePrefix = "ensemble-state-"
	stateLabel        = "ensemble.state"
	stateOwnerLabel   = "ensemble.state-owner" // its client dirs' uid:gid, which its readers run as
	readerLabel       = "ensemble.state-reader"
)

var (
	// A workspace may not equal or contain these container paths.
	covered = []string{"/", "/usr", "/etc", "/opt", "/run", "/home", "/tmp", "/proc", "/sys", "/dev"}
	// Nor lie inside these: a volume there would replace what the image's root-run steps execute.
	internal = []string{"/usr", "/etc", "/opt/ensemble", "/run", "/proc", "/sys", "/dev", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32",
		"/var/run", "/var/lock", "/home/agent"}
)

func inside(p, base string) bool {
	return p == base || base == "/" || strings.HasPrefix(p, base+"/")
}

func checkTree(p string, code protocol.Code) error {
	for _, c := range covered {
		if inside(c, p) {
			return protocol.Newf(code, "Workspace %s equals or contains the container's %s.", p, c)
		}
	}
	for _, c := range internal {
		if inside(p, c) {
			return protocol.Newf(code, "Workspace %s lies inside the container's %s.", p, c)
		}
	}
	return nil
}

// realSafe requires an existing directory reached without any symlink component.
func realSafe(path string) error {
	p := "/"
	for _, part := range strings.Split(strings.Trim(path, "/"), "/") {
		p = filepath.Join(p, part)
		info, err := os.Lstat(p)
		if err != nil {
			return err
		}
		if info.Mode()&os.ModeSymlink != 0 {
			return fmt.Errorf("%s is a symlink", p)
		}
	}
	if info, err := os.Stat(path); err != nil || !info.IsDir() {
		return fmt.Errorf("%s is not a directory", path)
	}
	return nil
}

func isDir(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.IsDir()
}

func (h Host) checkMounts(m Manifest) error {
	reject := func(format string, args ...any) error {
		return protocol.Newf(protocol.MountRejected, format, args...)
	}
	if !h.IsDir(m.CWD) {
		return reject("Workspace %s must be an existing host directory.", m.CWD)
	}
	if err := checkTree(m.CWD, protocol.MountRejected); err != nil {
		return err
	}
	dir := m.StateDir()
	if want := ClientHomes + "/" + m.Client; dir != want {
		return reject("%s_dir must be %s.", m.Client, want)
	}
	if inside(m.CWD, dir) || inside(dir, m.CWD) {
		return reject("Workspace %s overlaps %s_dir %s.", m.CWD, m.Client, dir)
	}
	if m.HasHook {
		for _, part := range strings.Split(m.HookPath[1:], "/") {
			if part == "" || part == "." || part == ".." {
				return reject("hook_path %s must be a clean absolute path.", m.HookPath)
			}
		}
		for _, mount := range []string{m.CWD, dir, docker.RunDir} {
			if inside(m.HookPath, mount) {
				return reject("hook_path %s lies inside the mount %s.", m.HookPath, mount)
			}
		}
		if inside(m.CWD, m.HookPath) {
			return reject("Workspace %s lies inside hook_path %s.", m.CWD, m.HookPath)
		}
	}
	return nil
}

// mountSpec: Docker parses a --mount value as one CSV record.
func mountSpec(fields ...string) string {
	var b strings.Builder
	w := csv.NewWriter(&b)
	_ = w.Write(fields)
	w.Flush()
	return strings.TrimSuffix(b.String(), "\n")
}

// stateMount: Docker requires the subpath to exist in the volume.
func stateMount(volume, subpath, target string) string {
	return mountSpec("type=volume", "source="+volume, "target="+target, "volume-subpath="+subpath, "volume-nocopy")
}

const workspacePrefix = "ensemble-ws-"

// sessionVolumes are a session's workspace and client state volumes, found by name alone.
type sessionVolumes struct{ Workspace, State string }

func volumesOf(name string) sessionVolumes {
	return sessionVolumes{Workspace: workspacePrefix + name, State: stateVolumePrefix + name}
}

// sessionOf is the session whose workspace volume is named workspace.
func sessionOf(workspace string) (string, bool) {
	name, ok := strings.CutPrefix(workspace, workspacePrefix)
	if !ok || name == "" {
		return "", false
	}
	return name, true
}

// stateDirs are the subpaths m's mounts name, parents first.
func stateDirs(m Manifest) []string {
	if m.Client != "codex" {
		return []string{m.Client}
	}
	dirs := []string{m.Client}
	for _, kept := range codexKept {
		dirs = append(dirs, m.Client+"/"+kept)
	}
	return dirs
}

// volumeMount uses volume-nocopy: otherwise a container on the empty volume copies its root-made mount point onto the
// volume root, undoing ensureVolume's chown.
func volumeMount(volume, target string, readonly bool) string {
	fields := []string{"type=volume", "source=" + volume, "target=" + target}
	if readonly {
		fields = append(fields, "readonly")
	}
	return mountSpec(append(fields, "volume-nocopy")...)
}

func workspaceMount(volume, target string, readonly bool) (string, audit.Mount) {
	mode := "rw"
	if readonly {
		mode = "ro"
	}
	return volumeMount(volume, target, readonly), audit.Mount{Type: "volume", Source: volume, Target: target, Mode: mode}
}

func (h Host) volumeLabels(ctx context.Context, d Daemon, volume string) (map[string]string, error) {
	out, err := d.Runner.Run(ctx, []string{"volume", "inspect", "--format", "{{json .Labels}}", volume}, nil)
	if err != nil {
		return nil, err
	}
	var labels map[string]string
	if err := json.Unmarshal(out, &labels); err != nil {
		return nil, fmt.Errorf("volume %s labels: %w", volume, err)
	}
	return labels, nil
}

func randomHex() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

// removeVolume runs on a context of its own; a failed removal adds the command to e.
func (h Host) removeVolume(ctx context.Context, d Daemon, v string, e *protocol.Error) {
	rctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 30*time.Second)
	defer cancel()
	if _, err := d.Runner.Run(rctx, []string{"volume", "rm", v}, nil); err != nil {
		e.Message += " Remove it with docker volume rm " + v + " before retrying."
	}
}

// ensureVolume: an existing workspace must match its labels and have no container mounting it; a prefetching start
// knows it created the volume by its own nonce.
func (h Host) ensureVolume(ctx context.Context, d Daemon, m Manifest, info imageInfo) (created bool, _ error) {
	v := m.Workspace
	labels, err := h.volumeLabels(ctx, d, v)
	created = docker.Absent(err)
	if created {
		create := []string{"volume", "create", "--label", kindLabel + "=" + kind, "--label", workspaceLabel + "=" + m.CWD}
		nonce := ""
		if m.Prefetch != nil {
			nonce = randomHex()
			create = append(create, "--label", nonceLabel+"="+nonce)
		}
		if _, err := d.Runner.Run(ctx, append(create, v), nil); err != nil {
			return false, failure(err, protocol.StartFailed, "Creating workspace volume "+v+" failed.")
		}
		// create succeeds on a name a racing start made first, so read back what exists.
		labels, err = h.volumeLabels(ctx, d, v)
		if err == nil && nonce != "" {
			created = labels[nonceLabel] == nonce
		}
	}
	if err != nil {
		return h.undo(ctx, startVolume{d, v, created}, failure(err, protocol.StartFailed, "Cannot inspect workspace volume "+v+"."))
	}
	if labels[kindLabel] != kind || labels[workspaceLabel] != m.CWD {
		return false, protocol.Newf(protocol.MountRejected, "Volume %s is not the workspace for %s (%s %q, %s %q).",
			v, m.CWD, kindLabel, labels[kindLabel], workspaceLabel, labels[workspaceLabel])
	}
	out, err := d.Runner.Run(ctx, []string{"ps", "--filter", "volume=" + v, "--format", "{{.Names}}"}, nil)
	if err != nil {
		return h.undo(ctx, startVolume{d, v, created}, failure(err, protocol.StartFailed, "Cannot list the containers using "+v+"."))
	}
	if users := strings.Fields(string(out)); len(users) > 0 {
		return false, protocol.Newf(protocol.MountRejected, "Workspace volume %s is mounted by running container %s; close it first.", v, users[0])
	}
	if !created {
		return false, nil
	}
	l := info.Config.Labels
	chown := []string{"run", "--rm", "--network", "none", "--cap-drop", "ALL", "--cap-add", "CHOWN", "--security-opt", "no-new-privileges", "--user", "0:0",
		"--mount", volumeMount(v, "/workspace", false), "--entrypoint", "/bin/chown", info.ID, l["ensemble.agent-uid"] + ":" + l["ensemble.agent-gid"], "/workspace"}
	if _, err := d.Runner.Run(ctx, chown, nil); err != nil {
		return h.undo(ctx, startVolume{d, v, created}, failure(err, protocol.StartFailed, "Giving workspace volume "+v+" to agent failed."))
	}
	return true, nil
}

func (h Host) ensureStateVolume(ctx context.Context, d Daemon, m Manifest, info imageInfo) (created bool, _ error) {
	v := volumesOf(m.Name).State
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
func (h Host) removeStateVolume(ctx context.Context, d Daemon, name string) error {
	v := volumesOf(name).State
	out, err := d.Runner.Run(ctx, []string{"ps", "--all", "--quiet", "--filter", "label=" + readerLabel + "=" + name}, nil)
	if err != nil {
		return failure(err, protocol.WorkspaceFailed, "Cannot list the readers of "+v+".")
	}
	if readers := strings.Fields(string(out)); len(readers) > 0 {
		if _, err := d.Runner.Run(ctx, append([]string{"rm", "--force"}, readers...), nil); err != nil {
			return failure(err, protocol.WorkspaceFailed, "Removing the readers of "+v+" failed.")
		}
	}
	if _, err := d.Runner.Run(ctx, []string{"volume", "rm", v}, nil); err != nil && !docker.Absent(err) {
		return diagnosed(err, protocol.WorkspaceFailed, "Deleting volume "+v+" failed.")
	}
	return nil
}
