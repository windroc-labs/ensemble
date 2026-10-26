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
	"unicode"

	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/protocol"
)

//go:embed assets/*
var assets embed.FS

// checks run in an image before the ensemble layer is built on it; they never enter a build context.
//
//go:embed checks/*
var checks embed.FS

// checkPath is the ensemble layer's PATH less /opt/ensemble/bin: where the layer finds what it needs.
const checkPath = "/home/agent/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"

var baseImagePattern = regexp.MustCompile(`^(?:sha256:[a-f0-9]{64}|[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?)$`)

// ValidBaseImage reports whether s names a local image: a tag, repository[:tag], or sha256 ID (no registry port or
// digest).
func ValidBaseImage(s string) bool {
	return len(s) <= 255 && baseImagePattern.MatchString(s)
}

// baseRequirements are what the base check reports missing, in its order; agent is its non-root user agent.
var baseRequirements = []string{"apt-get", "dpkg", "node", "npm", "tmux", "git", "agent"}

// BuildSpec is what a build adds to the ensemble package.
type BuildSpec struct {
	BaseImage    string   // a local image as stage 1, never pulled or rebuilt; "": built from ~/.ensemble/docker/worker
	Plugins      []string // repository sources both clients install
	ImagePlugins []string // plugins the stage-1 image carries under /opt/windroc/plugins, for Claude Code
}

var digestPattern = regexp.MustCompile(`^sha256:[a-f0-9]{64}$`)
var versionPattern = regexp.MustCompile(`^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$`)
var builtPattern = regexp.MustCompile(`^ensemble-(?:user|worker):[0-9a-f]{64}$`)

// imageLease is how long prune keeps a key's images after a build last leased the key:
// through a rebuild in progress and the start that follows a build.
const imageLease = time.Hour

// variantLease is how long prune keeps, and check-clients refreshes, a recorded variant after a build last used it
// (its record's mtime, which only Build writes), so a variant idle for a while still starts without a rebuild.
const variantLease = 7 * 24 * time.Hour

type imageInfo struct {
	ID      string `json:"Id"`
	Created time.Time
	Config  struct {
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

// stage snapshots the worker image definition (none with a base image), package, plugin sources and embedded assets
// into tmp, and returns the package version and Claude Code's plugin directories. Key-only inputs go in tmp itself,
// outside every build context.
func (h Host) stage(pkg string, spec BuildSpec, tmp string) (string, string, error) {
	userDir, packageDir := filepath.Join(tmp, "user"), filepath.Join(tmp, "package")
	if spec.BaseImage == "" {
		definition := filepath.Join(h.Home, ".ensemble/docker/worker")
		if info, err := os.Stat(definition); err != nil || !info.IsDir() {
			return "", "", protocol.New(protocol.BuildFailed, "No worker image definition at "+definition+"; run sh <ensemble plugin directory>/src/broker/install.sh.")
		}
		if err := snapshotTree(definition, userDir); err != nil {
			return "", "", protocol.New(protocol.BuildFailed, "Cannot read the worker image definition.")
		}
	}
	for _, rel := range []string{"src/ensemble/session", "src/ensemble/utils", "src/ensemble/delegate", ".claude-plugin/plugin.json"} {
		if err := snapshotTree(filepath.Join(pkg, rel), filepath.Join(packageDir, rel)); err != nil {
			return "", "", protocol.New(protocol.BuildFailed, "Invalid ensemble package directory: "+rel+" unreadable.")
		}
	}
	var meta struct {
		Version string `json:"version"`
	}
	data, err := os.ReadFile(filepath.Join(packageDir, ".claude-plugin/plugin.json"))
	if err != nil || json.Unmarshal(data, &meta) != nil || !versionPattern.MatchString(meta.Version) {
		return "", "", protocol.New(protocol.BuildFailed, "Invalid ensemble package version in .claude-plugin/plugin.json.")
	}
	entries, err := assets.ReadDir("assets")
	if err != nil {
		return "", "", protocol.New(protocol.BuildFailed, "Embedded image assets unreadable.")
	}
	for _, entry := range entries {
		data, err := assets.ReadFile("assets/" + entry.Name())
		if err == nil {
			err = os.WriteFile(filepath.Join(packageDir, entry.Name()), data, 0o644)
		}
		if err != nil {
			return "", "", protocol.New(protocol.BuildFailed, "Cannot stage image assets.")
		}
	}
	dirs, err := stagePlugins(spec.Plugins, spec.ImagePlugins, packageDir)
	if err != nil {
		return "", "", err
	}
	if len(spec.ImagePlugins) > 0 {
		if err := os.WriteFile(filepath.Join(tmp, "image-plugins"), []byte(strings.Join(spec.ImagePlugins, "\n")+"\n"), 0o644); err != nil {
			return "", "", protocol.New(protocol.BuildFailed, "Cannot stage in-image plugins.")
		}
	}
	return meta.Version, dirs, nil
}

// prepared is what prepare derives: the key, the package version, Claude Code's plugin directories, the base image's
// ID ("" for none) and spec with its in-image plugins sorted.
type prepared struct {
	key, version, dirs, baseID string
	spec                       BuildSpec
}

// prepare stages spec's build context into tmp and derives its key; Build and CheckClients share it so a key is
// derived one way.
func (h Host) prepare(ctx context.Context, pkg string, spec BuildSpec, tmp string) (prepared, error) {
	spec.ImagePlugins = slices.Sorted(slices.Values(spec.ImagePlugins))
	version, dirs, err := h.stage(pkg, spec, tmp)
	if err != nil {
		return prepared{}, err
	}
	var base imageInfo
	if spec.BaseImage != "" {
		if base, err = h.baseImage(ctx, spec.BaseImage); err != nil {
			return prepared{}, err
		}
		if err := os.WriteFile(filepath.Join(tmp, "base-image"), []byte(base.ID+"\n"), 0o644); err != nil {
			return prepared{}, protocol.New(protocol.BuildFailed, "Cannot stage the base image.")
		}
	}
	key, err := treeDigest(tmp)
	if err != nil {
		return prepared{}, protocol.New(protocol.BuildFailed, "Cannot hash the build context.")
	}
	return prepared{key: key, version: version, dirs: dirs, baseID: base.ID, spec: spec}, nil
}

// Build produces ensemble-worker:<key> in two stages; an image with this key, version and client versions built less
// than maxAge ago is reused (maxAge < 0: of any age; 0: never). A rebuild installs each client's latest release; both
// stages rebuild without cache to refresh plugins. spec's plugins are part of the key.
func (h Host) Build(ctx context.Context, pkg string, spec BuildSpec, maxAge time.Duration) (BuildResult, error) {
	tmp, err := docker.TempDir(h.Home, "ensemble-build-")
	if err != nil {
		return BuildResult{}, protocol.New(protocol.BuildFailed, "Cannot create a build directory.")
	}
	defer os.RemoveAll(tmp)
	p, err := h.prepare(ctx, pkg, spec, tmp)
	if err != nil {
		return BuildResult{}, err
	}
	key, version, dirs, spec := p.key, p.version, p.dirs, p.spec
	image := "ensemble-worker:" + key
	info, found, err := h.leaseImage(ctx, key)
	if err != nil {
		return BuildResult{}, err
	}
	built := func(result BuildResult) (BuildResult, error) {
		h.record(key, pkg, spec)
		h.afterBuild()
		return result, nil
	}
	_, labelled := imageClients(info.Config.Labels)
	current := found && info.Config.Labels["ensemble.key"] == key && info.Config.Labels["ensemble.version"] == version && labelled
	if current && (maxAge < 0 || (maxAge > 0 && time.Since(info.Created) < maxAge)) {
		return built(BuildResult{Image: image, Digest: info.ID})
	}
	// An age-only refresh that fails (e.g. offline) keeps the usable image.
	keep := current && maxAge > 0
	kept := func(why string) (BuildResult, error) {
		h.note("kept " + image + ": refreshing it failed: " + why)
		return built(BuildResult{Image: image, Digest: info.ID})
	}
	latest, err := h.latestClients(ctx)
	if err != nil {
		if keep {
			return kept(err.Error())
		}
		return BuildResult{}, protocol.New(protocol.BuildFailed, "Cannot look up the latest client releases; check the network and retry.")
	}
	result, err := h.rebuild(ctx, tmp, key, version, dirs, spec, p.baseID, latest)
	var e *protocol.Error
	if keep && errors.As(err, &e) && e.Code == protocol.BuildFailed {
		why := e.Message
		if e.Cause != "" {
			why += " (" + e.Cause + ")"
		}
		return kept(why)
	}
	if err != nil {
		return BuildResult{}, err
	}
	return built(result)
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

// leased reports whether a build leased key less than d ago.
func (h Host) leased(key string, d time.Duration) bool {
	info, err := os.Stat(h.leasePath(key))
	return err == nil && h.now().Sub(info.ModTime()) < d
}

// kept reports whether prune keeps key's images: leased within imageLease, or a variant a build used within
// variantLease.
func (h Host) kept(key string) bool {
	return h.leased(key, imageLease) || h.inUse(key)
}

// inUse reports whether a build used key's recorded variant less than variantLease ago; a check rebuild's own leases
// never count, so an abandoned variant ages out.
func (h Host) inUse(key string) bool {
	info, err := os.Stat(h.recordPath(key))
	return err == nil && h.now().Sub(info.ModTime()) < variantLease
}

// rebuild builds both stages of key's images from the context in tmp, stage 1 being baseID's image when set, the
// ensemble layer installing the clients at v.
func (h Host) rebuild(ctx context.Context, tmp, key, version, plugins string, spec BuildSpec, baseID string, v ClientVersions) (BuildResult, error) {
	image, userImage := "ensemble-worker:"+key, "ensemble-user:"+key
	var uid, gid string
	var err error
	if baseID != "" {
		uid, gid, err = h.useBase(ctx, spec.BaseImage, baseID, userImage)
	} else {
		uid, gid, err = h.buildDefinition(ctx, tmp, key, userImage)
	}
	if err != nil {
		return BuildResult{}, err
	}
	if len(spec.ImagePlugins) > 0 {
		if err := h.checkImagePlugins(ctx, userImage, spec.ImagePlugins); err != nil {
			return BuildResult{}, err
		}
	}
	if err := h.lease(key); err != nil {
		return BuildResult{}, leaseFailed()
	}
	replaced, err := h.markReplaced(ctx, key, replacedImage{Pending: true})
	if err != nil {
		return BuildResult{}, err
	}
	args := []string{"build", "--progress=plain", "--no-cache", "--build-arg", "USER_IMAGE=" + userImage, "--build-arg", "ENSEMBLE_VERSION=" + version,
		"--build-arg", "AGENT_UID=" + uid, "--build-arg", "AGENT_GID=" + gid, "--build-arg", "ENSEMBLE_PLUGIN_DIRS=" + plugins,
		"--build-arg", "CLAUDE_VERSION=" + v.Claude, "--build-arg", "CODEX_VERSION=" + v.Codex, "--build-arg", "OPENCODE_VERSION=" + v.Opencode, "--label", "ensemble.key=" + key, "--tag", image, filepath.Join(tmp, "package")}
	if _, err := h.Docker.Run(ctx, args, nil); err != nil {
		return BuildResult{}, diagnosed(err, protocol.BuildFailed, "Building the ensemble image stage failed.")
	}
	// The grace counts from the retag, however long the build took.
	if replaced.ID != "" {
		if _, err := h.markReplaced(ctx, key, replacedImage{ID: replaced.ID, At: replaced.At}); err != nil {
			h.note("cannot refresh the replaced image record of " + image + ": " + message(err))
		}
	}
	info, found, err := h.inspectImage(ctx, image)
	if err != nil {
		return BuildResult{}, failure(err, protocol.BuildFailed, "Cannot inspect the built image.")
	}
	labels := info.Config.Labels
	clients, _ := imageClients(labels)
	if !found || labels["ensemble.key"] != key || labels["ensemble.version"] != version || labels["ensemble.agent-uid"] != uid || labels["ensemble.agent-gid"] != gid ||
		clients != v {
		return BuildResult{}, protocol.New(protocol.BuildFailed, "Built image labels do not match the package.")
	}
	if err := h.lease(key); err != nil {
		return BuildResult{}, leaseFailed()
	}
	h.prune(ctx, key)
	return BuildResult{Image: image, Digest: info.ID, Rebuilt: true}, nil
}

// retagGrace is how long prune keeps an image ID a rebuild took key's tag from: a start that inspected the tag just
// before keeps using that ID for every step. pendingGrace bounds an entry whose rebuild has not retagged yet.
const (
	retagGrace   = 10 * time.Minute
	pendingGrace = 2 * time.Hour
)

// replacedImage is an entry of images/<key>.replaced: an image ID a rebuild took key's tag from, and when; Pending
// while that rebuild still runs.
type replacedImage struct {
	ID      string    `json:"id"`
	At      time.Time `json:"at"`
	Pending bool      `json:"pending,omitempty"`
}

func (h Host) replacedPath(key string) string {
	return h.leasePath(key) + ".replaced"
}

// readReplaced is key's replaced entries younger than retagGrace, or pendingGrace while pending; an unreadable list is
// empty.
func (h Host) readReplaced(key string) []replacedImage {
	var entries []replacedImage
	if data, err := os.ReadFile(h.replacedPath(key)); err != nil || json.Unmarshal(data, &entries) != nil {
		return nil
	}
	now := h.now()
	return slices.DeleteFunc(entries, func(e replacedImage) bool {
		return now.Sub(e.At) >= retagGrace && (!e.Pending || now.Sub(e.At) >= pendingGrace)
	})
}

// markReplaced records, under the image lock, the ID key's worker tag names (prior's ID when set, replacing that entry)
// as replaced now, pending as prior is; it returns the entry, a zero one when the tag names no image.
func (h Host) markReplaced(ctx context.Context, key string, prior replacedImage) (replacedImage, error) {
	unlock, err := h.Lock()
	if err != nil {
		return replacedImage{}, protocol.New(protocol.BuildFailed, "Taking the image lock ~/.ensemble/docker/images.lock failed.")
	}
	defer unlock()
	entry := replacedImage{ID: prior.ID, At: h.now(), Pending: prior.Pending}
	if entry.ID == "" {
		info, found, err := h.inspectImage(ctx, "ensemble-worker:"+key)
		if err != nil {
			return replacedImage{}, failure(err, protocol.BuildFailed, "Cannot inspect the worker image.")
		}
		if !found {
			return replacedImage{}, nil
		}
		entry.ID = info.ID
	}
	entries := slices.DeleteFunc(h.readReplaced(key), func(e replacedImage) bool { return e.ID == prior.ID && e.At.Equal(prior.At) })
	data, err := json.Marshal(append(entries, entry))
	if err == nil {
		err = writePrivate(h.replacedPath(key), data)
	}
	if err != nil {
		return replacedImage{}, protocol.New(protocol.BuildFailed, "Cannot record the replaced image in ~/.ensemble/docker/images.")
	}
	return entry, nil
}

// buildDefinition builds ~/.ensemble/docker/worker's snapshot in tmp as userImage and returns its agent's ids.
func (h Host) buildDefinition(ctx context.Context, tmp, key, userImage string) (string, string, error) {
	if _, err := h.Docker.Run(ctx, []string{"build", "--progress=plain", "--no-cache", "--label", "ensemble.key=" + key, "--tag", userImage, filepath.Join(tmp, "user")}, nil); err != nil {
		return "", "", diagnosed(err, protocol.BuildFailed, "Building the worker image definition failed.")
	}
	out, err := h.Docker.Run(ctx, []string{"run", "--rm", "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
		"--entrypoint", "/bin/sh", userImage, "-c", "id -u agent && id -g agent"}, nil)
	if err != nil {
		return "", "", failure(err, protocol.BuildFailed, "The worker image has no usable user agent.")
	}
	ids := strings.Fields(string(out))
	if len(ids) != 2 || !numeric(ids[0]) || !numeric(ids[1]) {
		return "", "", protocol.New(protocol.BuildFailed, "The worker image's user agent must be a non-root user.")
	}
	return ids[0], ids[1], nil
}

// baseImage inspects ref, which must be a local image without reserved labels.
func (h Host) baseImage(ctx context.Context, ref string) (imageInfo, error) {
	if !ValidBaseImage(ref) {
		return imageInfo{}, protocol.New(protocol.BuildFailed, "Base image "+ref+": use a local image tag or sha256 ID.")
	}
	info, found, err := h.inspectImage(ctx, ref)
	if err != nil {
		return imageInfo{}, failure(err, protocol.BuildFailed, "Cannot inspect base image "+ref+".")
	}
	if !found {
		return imageInfo{}, protocol.New(protocol.BuildFailed, "Base image "+ref+" is not a local image; build or load it first (ensemble-broker never pulls it).")
	}
	var reserved []string
	for label := range info.Config.Labels {
		if strings.HasPrefix(label, "windroc.") || strings.HasPrefix(label, "ensemble.") {
			reserved = append(reserved, label)
		}
	}
	if len(reserved) == 0 {
		return info, nil
	}
	// Label keys come from the image: name a few, never echoing a control character or a long key.
	slices.Sort(reserved)
	shown := reserved[:min(5, len(reserved))]
	for i, label := range shown {
		if len(label) > 128 || strings.ContainsFunc(label, func(r rune) bool { return !unicode.IsPrint(r) }) {
			shown[i] = "(invalid label)"
		}
	}
	names := strings.Join(shown, ", ")
	if more := len(reserved) - len(shown); more > 0 {
		names += " and " + strconv.Itoa(more) + " more"
	}
	return imageInfo{}, protocol.New(protocol.BuildFailed, "Base image "+ref+" carries reserved labels "+names+"; remove every windroc. and ensemble. label.")
}

// useBase checks ref's image, baseID, meets the ensemble layer's needs, tags it userImage and returns its agent's ids.
func (h Host) useBase(ctx context.Context, ref, baseID, userImage string) (string, string, error) {
	unexpected := protocol.New(protocol.BuildFailed, "Checking base image "+ref+" gave unexpected output.")
	lines, err := h.baseCheck(ctx, ref, baseID, "65534:65534", "agent", "/usr/bin:/bin")
	if err != nil {
		return "", "", err
	}
	missing := map[string]bool{}
	var ids []string
	if fields := strings.Fields(strings.Join(lines, " ")); len(lines) == 1 && lines[0] == "missing agent" {
		missing["agent"] = true
	} else if len(lines) == 1 && len(fields) == 3 && fields[0] == "agent" && numeric(fields[1]) && numeric(fields[2]) {
		ids = fields[1:]
	} else {
		return "", "", unexpected
	}
	// Commands are found as agent, who runs them (its home may be private); without agent, as nobody.
	user := "65534:65534"
	if ids != nil {
		user = ids[0] + ":" + ids[1]
	}
	if lines, err = h.baseCheck(ctx, ref, baseID, user, "tools", checkPath); err != nil {
		return "", "", err
	}
	for _, line := range lines {
		tool, ok := strings.CutPrefix(line, "missing ")
		if !ok || tool == "agent" || !slices.Contains(baseRequirements, tool) || missing[tool] {
			return "", "", unexpected
		}
		missing[tool] = true
	}
	if len(missing) > 0 {
		var lacks []string
		for _, item := range baseRequirements {
			switch {
			case !missing[item]:
			case item == "agent":
				lacks = append(lacks, "a non-root user agent")
			default:
				lacks = append(lacks, item)
			}
		}
		return "", "", protocol.New(protocol.BuildFailed, "Base image "+ref+" lacks "+strings.Join(lacks, ", ")+"; see docs/docker-host.md#base-images.")
	}
	if _, err := h.Docker.Run(ctx, []string{"tag", baseID, userImage}, nil); err != nil {
		return "", "", diagnosed(err, protocol.BuildFailed, "Tagging base image "+ref+" failed.")
	}
	return ids[0], ids[1], nil
}

// baseCheck runs checks/base-check's mode with path in baseID's image as user, offline and without capabilities, and
// returns its output lines.
func (h Host) baseCheck(ctx context.Context, ref, baseID, user, mode, path string) ([]string, error) {
	script, err := checks.ReadFile("checks/base-check")
	if err != nil {
		return nil, protocol.New(protocol.BuildFailed, "Embedded image checks unreadable.")
	}
	out, err := h.Docker.Run(ctx, []string{"run", "--rm", "-i", "--pull", "never", "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
		"--user", user, "--entrypoint", "/bin/sh", baseID, "-s", mode, path}, script)
	if err != nil {
		return nil, diagnosed(err, protocol.BuildFailed, "Checking base image "+ref+" failed.")
	}
	if text := strings.TrimSuffix(string(out), "\n"); text != "" {
		return strings.Split(text, "\n"), nil
	}
	return nil, nil
}

// checkImagePlugins requires each of names to be a plugin directory agent can read in image, its
// plugin.json naming it.
func (h Host) checkImagePlugins(ctx context.Context, image string, names []string) error {
	script, err := checks.ReadFile("checks/plugin-check.js")
	if err != nil {
		return protocol.New(protocol.BuildFailed, "Embedded image checks unreadable.")
	}
	args := append([]string{"run", "--rm", "-i", "--pull", "never", "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
		"--user", "agent", "--env", "PATH=" + checkPath, "--entrypoint", "node", image, "-", strings.TrimSuffix(imagePluginRoot, "/")}, names...)
	out, err := h.Docker.Run(ctx, args, script)
	if err != nil {
		return diagnosed(err, protocol.BuildFailed, "Checking in-image plugins failed.")
	}
	unexpected := protocol.New(protocol.BuildFailed, "Checking in-image plugins gave unexpected output.")
	var lines, problems []string
	if text := strings.TrimSuffix(string(out), "\n"); text != "" {
		lines = strings.Split(text, "\n")
	}
	seen := map[string]bool{}
	for _, line := range lines {
		name, reason, _ := strings.Cut(line, " ")
		dir := imagePluginRoot + name + "/"
		if !slices.Contains(names, name) || seen[name] {
			return unexpected
		}
		seen[name] = true
		switch reason {
		case "missing":
			problems = append(problems, "In-image plugin "+name+": no "+dir+".claude-plugin/plugin.json.")
		case "unreadable":
			problems = append(problems, "In-image plugin "+name+": agent cannot read all of "+dir+".")
		case "misnamed":
			problems = append(problems, "In-image plugin "+name+": "+dir+`.claude-plugin/plugin.json must name it "`+name+`".`)
		default:
			return unexpected
		}
	}
	if len(problems) > 0 {
		return protocol.New(protocol.BuildFailed, strings.Join(problems, " "))
	}
	return nil
}

// prune removes the user and worker images of keys other than key ("": none) that it does not keep (kept),
// with a worker image its key's record and replaced list, and the untagged ones a same-key rebuild left, except one
// replaced within retagGrace, that no container, running or stopped, uses, noting each to Progress; whatever fails is
// kept and never fails the build. It holds the image lock, so a build's lease and inspect come wholly before or after
// it.
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
		if !builtPattern.MatchString(ref) || refKey == key || used[id] || h.kept(refKey) {
			continue
		}
		// No --force: Docker also refuses an image a container uses.
		if _, err := h.Docker.Run(ctx, []string{"image", "rm", ref}, nil); err != nil {
			h.note("kept " + ref + ": " + docker.Diagnostic(err))
		} else {
			h.note("removed " + ref)
			// A stale lease and none are alike: the lease goes with the first of its key's images.
			_ = os.Remove(h.leasePath(refKey))
			if strings.HasPrefix(ref, "ensemble-worker:") {
				_ = os.Remove(h.recordPath(refKey))
				_ = os.Remove(h.replacedPath(refKey))
			}
		}
	}
	replaced := map[string]bool{}
	entries, _ := os.ReadDir(h.dockerPath("images"))
	for _, entry := range entries {
		if m := variantFile.FindStringSubmatch(entry.Name()); m != nil && m[2] == "replaced" {
			for _, r := range h.readReplaced(m[1]) {
				replaced[r.ID] = true
			}
		}
	}
	out, err = h.Docker.Run(ctx, []string{"image", "ls", "--quiet", "--no-trunc", "--filter", "dangling=true", "--filter", "label=ensemble.key"}, nil)
	if err != nil {
		h.note("kept untagged images: cannot list them: " + docker.Diagnostic(err))
		return
	}
	for _, id := range strings.Fields(string(out)) {
		if used[id] || replaced[id] {
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
