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
	"strings"

	"ensemble/broker/internal/audit"
	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/protocol"
)

var (
	// A workspace may not equal or contain these container paths.
	covered = []string{"/", "/usr", "/etc", "/opt", "/run", "/home", "/tmp", "/proc", "/sys", "/dev"}
	// Nor lie inside these: a volume there would replace what the image's root-run steps execute.
	internal = []string{"/usr", "/etc", "/opt/ensemble", "/run", "/proc", "/sys", "/dev", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32",
		"/var/run", "/var/lock", "/home/agent"}
)

// inside reports whether path p is base or below it, by components.
func inside(p, base string) bool {
	return p == base || base == "/" || strings.HasPrefix(p, base+"/")
}

// checkTree applies the mount-point rules a clean absolute container path can be judged on alone, failing with code.
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

// checkMounts applies the mount rules; cwd is a mount point, never a bind source.
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
	if want := filepath.Join(h.Home, ".ensemble/docker-state", m.Name, m.Client); dir != want {
		return reject("%s_dir must be %s.", m.Client, want)
	}
	if err := h.Safe(dir); err != nil {
		return reject("%s_dir %s must be an existing directory without symlinks: %v.", m.Client, dir, err)
	}
	if m.Client == "codex" {
		if err := h.Safe(filepath.Join(dir, "sessions")); err != nil {
			return reject("codex sessions must be an existing directory without symlinks.")
		}
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

// mountSpec renders a --mount value; Docker parses it as one CSV record.
func mountSpec(fields ...string) string {
	var b strings.Builder
	w := csv.NewWriter(&b)
	_ = w.Write(fields)
	w.Flush()
	return strings.TrimSuffix(b.String(), "\n")
}

func bindMount(source, target string) string {
	return mountSpec("type=bind", "source="+source, "target="+target)
}

// volumeMount mounts a workspace volume with volume-nocopy: without it, a container created on the still-empty volume
// copies its mount point (the --workdir Docker made as root) onto the volume root, undoing ensureVolume's chown.
func volumeMount(volume, target string, readonly bool) string {
	fields := []string{"type=volume", "source=" + volume, "target=" + target}
	if readonly {
		fields = append(fields, "readonly")
	}
	return mountSpec(append(fields, "volume-nocopy")...)
}

// workspaceMount is volumeMount's value with its audit record, both from the same readonly.
func workspaceMount(volume, target string, readonly bool) (string, audit.Mount) {
	mode := "rw"
	if readonly {
		mode = "ro"
	}
	return volumeMount(volume, target, readonly), audit.Mount{Type: "volume", Source: volume, Target: target, Mode: mode}
}

// volumeLabels reads a volume's labels; an absent volume is an error docker.Absent recognizes.
func (h Host) volumeLabels(ctx context.Context, volume string) (map[string]string, error) {
	out, err := h.Docker.Run(ctx, []string{"volume", "inspect", "--format", "{{json .Labels}}", volume}, nil)
	if err != nil {
		return nil, err
	}
	var labels map[string]string
	if err := json.Unmarshal(out, &labels); err != nil {
		return nil, fmt.Errorf("volume %s labels: %w", volume, err)
	}
	return labels, nil
}

// ensureVolume gives m its workspace volume: created with its labels and an agent-owned root, or an
// existing one whose labels match and that no container mounts. created: this start made it, which a prefetching
// start knows by its own nonceLabel value.
func (h Host) ensureVolume(ctx context.Context, m Manifest, info imageInfo) (created bool, _ error) {
	v := m.Workspace
	labels, err := h.volumeLabels(ctx, v)
	created = docker.Absent(err)
	if created {
		create := []string{"volume", "create", "--label", kindLabel + "=" + kind, "--label", workspaceLabel + "=" + m.CWD}
		nonce := ""
		if m.Prefetch != nil {
			id := make([]byte, 16)
			_, _ = rand.Read(id)
			nonce = hex.EncodeToString(id)
			create = append(create, "--label", nonceLabel+"="+nonce)
		}
		if _, err := h.Docker.Run(ctx, append(create, v), nil); err != nil {
			return false, failure(err, protocol.StartFailed, "Creating workspace volume "+v+" failed.")
		}
		// create succeeds on a name a racing start made first, so read back what exists.
		labels, err = h.volumeLabels(ctx, v)
		if err == nil && nonce != "" {
			created = labels[nonceLabel] == nonce
		}
	}
	// Until its root is the agent's, a volume this start created is removed on failure: a later start would skip the chown.
	undo := func(e *protocol.Error) (bool, error) {
		if created {
			if _, rmErr := h.Docker.Run(context.WithoutCancel(ctx), []string{"volume", "rm", v}, nil); rmErr != nil {
				e.Message += " Remove it with docker volume rm " + v + " before retrying."
			}
		}
		return false, e
	}
	if err != nil {
		return undo(failure(err, protocol.StartFailed, "Cannot inspect workspace volume "+v+"."))
	}
	if labels[kindLabel] != kind || labels[workspaceLabel] != m.CWD {
		return false, protocol.Newf(protocol.MountRejected, "Volume %s is not the workspace for %s (%s %q, %s %q).",
			v, m.CWD, kindLabel, labels[kindLabel], workspaceLabel, labels[workspaceLabel])
	}
	out, err := h.Docker.Run(ctx, []string{"ps", "--filter", "volume=" + v, "--format", "{{.Names}}"}, nil)
	if err != nil {
		return undo(failure(err, protocol.StartFailed, "Cannot list the containers using "+v+"."))
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
	if _, err := h.Docker.Run(ctx, chown, nil); err != nil {
		return undo(failure(err, protocol.StartFailed, "Giving workspace volume "+v+" to agent failed."))
	}
	return true, nil
}
