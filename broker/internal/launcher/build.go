package launcher

import (
	"context"
	"crypto/sha256"
	"embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"

	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/protocol"
)

//go:embed assets/*
var assets embed.FS

var digestPattern = regexp.MustCompile(`^sha256:[a-f0-9]{64}$`)
var versionPattern = regexp.MustCompile(`^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$`)
var builtPattern = regexp.MustCompile(`^ensemble-(?:user|worker):[0-9a-f]{64}$`)

// imageLease is how long prune keeps a key's images after a build last leased the key:
// through a rebuild in progress and the start that follows a build.
const imageLease = time.Hour

type imageInfo struct {
	ID           string `json:"Id"`
	Created      time.Time
	Architecture string
	Config       struct {
		Labels map[string]string
	}
}

// snapshotTree copies src to dst, less Python caches and the src-relative paths in skip.
func snapshotTree(src, dst string, skip ...string) error {
	return filepath.WalkDir(src, func(path string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(src, path)
		if err != nil {
			return err
		}
		if entry.Name() == "__pycache__" || (entry.IsDir() && slices.Contains(skip, rel)) {
			return filepath.SkipDir
		}
		if strings.HasSuffix(entry.Name(), ".pyc") || slices.Contains(skip, rel) {
			return nil
		}
		target := filepath.Join(dst, rel)
		info, err := entry.Info()
		if err != nil {
			return err
		}
		switch {
		case info.IsDir():
			return os.MkdirAll(target, info.Mode().Perm())
		case info.Mode()&os.ModeSymlink != 0:
			link, err := os.Readlink(path)
			if err != nil {
				return err
			}
			if err := os.Remove(target); err != nil && !os.IsNotExist(err) {
				return err
			}
			return os.Symlink(link, target)
		case info.Mode().IsRegular():
			in, err := os.Open(path)
			if err != nil {
				return err
			}
			defer in.Close()
			if err := os.MkdirAll(filepath.Dir(target), 0755); err != nil {
				return err
			}
			out, err := os.OpenFile(target, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, info.Mode().Perm())
			if err != nil {
				return err
			}
			_, copyErr := io.Copy(out, in)
			closeErr := out.Close()
			return errors.Join(copyErr, closeErr)
		default:
			return fmt.Errorf("unsupported build context file: %s", path)
		}
	})
}

func treeDigest(root string) (string, error) {
	h := sha256.New()
	err := filepath.WalkDir(root, func(path string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		fmt.Fprintf(h, "%d:%s:%o:", len(rel), rel, info.Mode())
		switch {
		case info.Mode().IsRegular():
			file, err := os.Open(path)
			if err != nil {
				return err
			}
			defer file.Close()
			fmt.Fprintf(h, "%d:", info.Size())
			_, err = io.Copy(h, file)
			return err
		case info.Mode()&os.ModeSymlink != 0:
			link, err := os.Readlink(path)
			fmt.Fprintf(h, "%d:%s", len(link), link)
			return err
		}
		return nil
	})
	return hex.EncodeToString(h.Sum(nil)), err
}

// inspectImage returns found=false when Docker reports the image absent.
func (h Host) inspectImage(ctx context.Context, image string) (imageInfo, bool, error) {
	out, err := h.Docker.Run(ctx, []string{"image", "inspect", image}, nil)
	if docker.Absent(err) {
		return imageInfo{}, false, nil
	}
	if err != nil {
		return imageInfo{}, false, err
	}
	var infos []imageInfo
	if err := json.Unmarshal(out, &infos); err != nil || len(infos) != 1 || !digestPattern.MatchString(infos[0].ID) {
		return imageInfo{}, false, errors.New("invalid image inspection")
	}
	return infos[0], true, nil
}

func numeric(s string) bool {
	n, err := strconv.ParseUint(s, 10, 31)
	return err == nil && n > 0
}

// checkImage requires a broker-built image: matching key, version and agent ids in its labels.
func (h Host) checkImage(ctx context.Context, image string) (imageInfo, error) {
	info, found, err := h.inspectImage(ctx, image)
	if err != nil {
		return imageInfo{}, failure(err, protocol.ImageRejected, "Cannot inspect image "+image+".")
	}
	l := info.Config.Labels
	if !found || l["ensemble.key"] != strings.TrimPrefix(image, "ensemble-worker:") || l["ensemble.version"] == "" ||
		!numeric(l["ensemble.agent-uid"]) || !numeric(l["ensemble.agent-gid"]) {
		return imageInfo{}, protocol.New(protocol.ImageRejected, "Image "+image+" is not a broker-built worker image; run ensemble-broker build.")
	}
	return info, nil
}

// stage snapshots the worker image definition, package, plugin sources and embedded assets into tmp.
func (h Host) stage(pkg string, plugins []string, tmp string) (string, []string, error) {
	userDir, packageDir := filepath.Join(tmp, "user"), filepath.Join(tmp, "package")
	definition := filepath.Join(h.Home, ".ensemble/docker/worker")
	if info, err := os.Stat(definition); err != nil || !info.IsDir() {
		return "", nil, protocol.New(protocol.BuildFailed, "No worker image definition at "+definition+"; run sh <ensemble plugin directory>/broker/install.sh.")
	}
	if err := snapshotTree(definition, userDir); err != nil {
		return "", nil, protocol.New(protocol.BuildFailed, "Cannot read the worker image definition.")
	}
	for _, rel := range []string{"session/src", "utils/src", "delegate/src", ".claude-plugin/plugin.json"} {
		if err := snapshotTree(filepath.Join(pkg, rel), filepath.Join(packageDir, rel)); err != nil {
			return "", nil, protocol.New(protocol.BuildFailed, "Invalid ensemble package directory: "+rel+" unreadable.")
		}
	}
	var meta struct {
		Version string `json:"version"`
	}
	data, err := os.ReadFile(filepath.Join(packageDir, ".claude-plugin/plugin.json"))
	if err != nil || json.Unmarshal(data, &meta) != nil || !versionPattern.MatchString(meta.Version) {
		return "", nil, protocol.New(protocol.BuildFailed, "Invalid ensemble package version in .claude-plugin/plugin.json.")
	}
	entries, err := assets.ReadDir("assets")
	if err != nil {
		return "", nil, protocol.New(protocol.BuildFailed, "Embedded image assets unreadable.")
	}
	for _, entry := range entries {
		data, err := assets.ReadFile("assets/" + entry.Name())
		if err == nil {
			err = os.WriteFile(filepath.Join(packageDir, entry.Name()), data, 0o644)
		}
		if err != nil {
			return "", nil, protocol.New(protocol.BuildFailed, "Cannot stage image assets.")
		}
	}
	staged, err := stagePlugins(plugins, packageDir)
	if err != nil {
		return "", nil, err
	}
	return meta.Version, staged, nil
}

// Build produces ensemble-worker:<key> in two stages; an image with this key and version built less than maxAge ago
// is reused (maxAge < 0: of any age; 0: never). Both stages rebuild without cache to refresh clients and plugins.
// plugins name repository sources the image installs independently; they are part of the key.
func (h Host) Build(ctx context.Context, pkg string, plugins []string, maxAge time.Duration) (BuildResult, error) {
	tmp, err := docker.TempDir(h.Home, "ensemble-build-")
	if err != nil {
		return BuildResult{}, protocol.New(protocol.BuildFailed, "Cannot create a build directory.")
	}
	defer os.RemoveAll(tmp)
	version, staged, err := h.stage(pkg, plugins, tmp)
	if err != nil {
		return BuildResult{}, err
	}
	key, err := treeDigest(tmp)
	if err != nil {
		return BuildResult{}, protocol.New(protocol.BuildFailed, "Cannot hash the build context.")
	}
	image := "ensemble-worker:" + key
	info, found, err := h.leaseImage(ctx, key)
	if err != nil {
		return BuildResult{}, err
	}
	current := found && info.Config.Labels["ensemble.key"] == key && info.Config.Labels["ensemble.version"] == version
	if current && (maxAge < 0 || (maxAge > 0 && time.Since(info.Created) < maxAge)) {
		return BuildResult{Image: image, Digest: info.ID}, nil
	}
	result, err := h.rebuild(ctx, tmp, key, version, pluginDirs(staged))
	var e *protocol.Error
	if current && maxAge > 0 && errors.As(err, &e) && e.Code == protocol.BuildFailed {
		// An age-only refresh that fails (e.g. offline) keeps the usable image.
		why := e.Message
		if e.Cause != "" {
			why += " (" + e.Cause + ")"
		}
		h.note("kept " + image + ": refreshing it failed: " + why)
		return BuildResult{Image: image, Digest: info.ID}, nil
	}
	return result, err
}

// leaseImage leases key, then inspects its worker image, both under the image lock:
// a concurrent prune either finished before the lease or keeps the image.
func (h Host) leaseImage(ctx context.Context, key string) (imageInfo, bool, error) {
	unlock, err := h.Lock()
	if err != nil {
		return imageInfo{}, false, protocol.New(protocol.BuildFailed, "Taking the image lock ~/.ensemble/docker/images.lock failed.")
	}
	defer unlock()
	if err := h.lease(key); err != nil {
		return imageInfo{}, false, leaseFailed()
	}
	info, found, err := h.inspectImage(ctx, "ensemble-worker:"+key)
	if err != nil {
		return imageInfo{}, false, failure(err, protocol.BuildFailed, "Cannot inspect the worker image.")
	}
	return info, found, nil
}

func leaseFailed() *protocol.Error {
	return protocol.New(protocol.BuildFailed, "Cannot write the image lease in ~/.ensemble/docker/images.")
}

func (h Host) leasePath(key string) string {
	return filepath.Join(h.Home, ".ensemble/docker/images", key)
}

// lease marks key's images as in use by a build from now on.
func (h Host) lease(key string) error {
	path := h.leasePath(key)
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	now := time.Now()
	return os.Chtimes(path, now, now)
}

// leased reports whether a build leased key less than imageLease ago.
func (h Host) leased(key string) bool {
	info, err := os.Stat(h.leasePath(key))
	return err == nil && time.Since(info.ModTime()) < imageLease
}

// rebuild builds both stages of key's images from the context in tmp.
func (h Host) rebuild(ctx context.Context, tmp, key, version, plugins string) (BuildResult, error) {
	image, userImage := "ensemble-worker:"+key, "ensemble-user:"+key
	if _, err := h.Docker.Run(ctx, []string{"build", "--progress=plain", "--no-cache", "--label", "ensemble.key=" + key, "--tag", userImage, filepath.Join(tmp, "user")}, nil); err != nil {
		return BuildResult{}, diagnosed(err, protocol.BuildFailed, "Building the worker image definition failed.")
	}
	out, err := h.Docker.Run(ctx, []string{"run", "--rm", "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
		"--entrypoint", "/bin/sh", userImage, "-c", "id -u agent && id -g agent"}, nil)
	if err != nil {
		return BuildResult{}, failure(err, protocol.BuildFailed, "The worker image has no usable user agent.")
	}
	ids := strings.Fields(string(out))
	if len(ids) != 2 || !numeric(ids[0]) || !numeric(ids[1]) {
		return BuildResult{}, protocol.New(protocol.BuildFailed, "The worker image's user agent must be a non-root user.")
	}
	uid, gid := ids[0], ids[1]
	if err := h.lease(key); err != nil {
		return BuildResult{}, leaseFailed()
	}
	args := []string{"build", "--progress=plain", "--no-cache", "--build-arg", "USER_IMAGE=" + userImage, "--build-arg", "ENSEMBLE_VERSION=" + version,
		"--build-arg", "AGENT_UID=" + uid, "--build-arg", "AGENT_GID=" + gid, "--build-arg", "ENSEMBLE_PLUGIN_DIRS=" + plugins, "--label", "ensemble.key=" + key, "--tag", image, filepath.Join(tmp, "package")}
	if _, err := h.Docker.Run(ctx, args, nil); err != nil {
		return BuildResult{}, diagnosed(err, protocol.BuildFailed, "Building the ensemble image stage failed.")
	}
	info, found, err := h.inspectImage(ctx, image)
	if err != nil {
		return BuildResult{}, failure(err, protocol.BuildFailed, "Cannot inspect the built image.")
	}
	labels := info.Config.Labels
	if !found || labels["ensemble.key"] != key || labels["ensemble.version"] != version || labels["ensemble.agent-uid"] != uid || labels["ensemble.agent-gid"] != gid {
		return BuildResult{}, protocol.New(protocol.BuildFailed, "Built image labels do not match the package.")
	}
	if err := h.lease(key); err != nil {
		return BuildResult{}, leaseFailed()
	}
	h.prune(ctx, key)
	return BuildResult{Image: image, Digest: info.ID, Rebuilt: true}, nil
}

// prune removes the user and worker images of keys other than key that no build leased within imageLease, and the
// untagged ones a same-key rebuild left, that no container, running or stopped, uses, noting each to Progress;
// whatever fails is kept and never fails the build. It holds the image lock, so a build's lease and inspect come
// wholly before or after it.
func (h Host) prune(ctx context.Context, key string) {
	unlock, err := h.Lock()
	if err != nil {
		h.note("kept older images: cannot take the image lock: " + err.Error())
		return
	}
	defer unlock()
	used, err := h.usedImages(ctx)
	if err != nil {
		h.note("kept older images: cannot list containers: " + docker.Diagnostic(err))
		return
	}
	out, err := h.Docker.Run(ctx, []string{"image", "ls", "--no-trunc", "--format", "{{.Repository}}:{{.Tag}} {{.ID}}"}, nil)
	if err != nil {
		h.note("kept older images: cannot list images: " + docker.Diagnostic(err))
		return
	}
	for _, line := range strings.Split(string(out), "\n") {
		ref, id, _ := strings.Cut(line, " ")
		_, refKey, _ := strings.Cut(ref, ":")
		if !builtPattern.MatchString(ref) || refKey == key || used[id] || h.leased(refKey) {
			continue
		}
		// No --force: Docker also refuses an image a container uses.
		if _, err := h.Docker.Run(ctx, []string{"image", "rm", ref}, nil); err != nil {
			h.note("kept " + ref + ": " + docker.Diagnostic(err))
		} else {
			h.note("removed " + ref)
			// A stale lease and none are alike: the lease goes with the first of its key's images.
			_ = os.Remove(h.leasePath(refKey))
		}
	}
	out, err = h.Docker.Run(ctx, []string{"image", "ls", "--quiet", "--no-trunc", "--filter", "dangling=true", "--filter", "label=ensemble.key"}, nil)
	if err != nil {
		h.note("kept untagged images: cannot list them: " + docker.Diagnostic(err))
		return
	}
	for _, id := range strings.Fields(string(out)) {
		if used[id] {
			continue
		}
		if _, err := h.Docker.Run(ctx, []string{"image", "rm", id}, nil); err != nil {
			h.note("kept " + id + ": " + docker.Diagnostic(err))
		} else {
			h.note("removed " + id)
		}
	}
}

func (h Host) note(s string) {
	if h.Progress != nil {
		fmt.Fprintln(h.Progress, "ensemble-broker: "+s)
	}
}

// usedImages is the set of image IDs every container, running or stopped, was created from.
func (h Host) usedImages(ctx context.Context) (map[string]bool, error) {
	out, err := h.Docker.Run(ctx, []string{"container", "ls", "--all", "--quiet", "--no-trunc"}, nil)
	ids := strings.Fields(string(out))
	if err != nil || len(ids) == 0 {
		return nil, err
	}
	out, err = h.Docker.Run(ctx, append([]string{"container", "inspect", "--format", "{{.Image}}"}, ids...), nil)
	used := map[string]bool{}
	for _, id := range strings.Fields(string(out)) {
		used[id] = true
	}
	return used, err
}
