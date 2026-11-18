package launcher

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"syscall"
	"time"

	"ensemble/broker/internal/codex"
	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/protocol"
)

// claudeMinVersion is the lowest Claude Code a build installs.
const claudeMinVersion = "2.1.0"

// clientCheckInterval is how often builds spawn check-clients.
const clientCheckInterval = time.Hour

// Releases is the npm registry's side of a build: a client package's latest stable release.
type Releases interface {
	Latest(ctx context.Context, pkg string) (string, error)
}

// ClientVersions are the client versions an image carries in its labels, each an exact stable X.Y.Z.
type ClientVersions struct{ Claude, Codex, Opencode string }

// clientPackages are the image's clients, in ClientVersions' field order.
var clientPackages = [...]struct{ name, pkg, label, min string }{
	{"claude", "@anthropic-ai/claude-code", "ensemble.claude-version", claudeMinVersion},
	{"codex", "@openai/codex", "ensemble.codex-version", codex.MinVersion},
	{"opencode", "opencode-ai", "ensemble.opencode-version", opencodeMinVersion},
}

func (v *ClientVersions) fields() [len(clientPackages)]*string {
	return [...]*string{&v.Claude, &v.Codex, &v.Opencode}
}

// MarshalJSON writes v as an object keyed by client name.
func (v ClientVersions) MarshalJSON() ([]byte, error) {
	named := map[string]string{}
	for i, field := range v.fields() {
		named[clientPackages[i].name] = *field
	}
	return json.Marshal(named)
}

// latestClients looks up every client's latest release within 2 minutes; each must be at least its minimum.
func (h Host) latestClients(ctx context.Context) (ClientVersions, error) {
	if h.Releases == nil {
		return ClientVersions{}, errors.New("no client release lookup")
	}
	ctx, cancel := context.WithTimeout(ctx, 2*time.Minute)
	defer cancel()
	var v ClientVersions
	for i, field := range v.fields() {
		c := clientPackages[i]
		latest, err := h.Releases.Latest(ctx, c.pkg)
		if err != nil {
			return ClientVersions{}, err
		}
		if !codex.ValidVersion(latest) {
			return ClientVersions{}, errors.New("invalid latest " + c.pkg + " release")
		}
		if codex.Newer(c.min, latest) {
			return ClientVersions{}, errors.New("latest " + c.pkg + " release " + latest + " is below " + c.min)
		}
		// The session's OpenCode client runs only 1.x: src/ensemble/session/clients/opencode/common.py MAJOR.
		if c.name == "opencode" && !strings.HasPrefix(latest, "1.") {
			return ClientVersions{}, errors.New("latest " + c.pkg + " release " + latest + " is not OpenCode 1.x")
		}
		*field = latest
	}
	return v, nil
}

// imageClients reads an image's client versions from its labels; ok only when all three are valid.
func imageClients(labels map[string]string) (ClientVersions, bool) {
	var v ClientVersions
	for i, field := range v.fields() {
		*field = labels[clientPackages[i].label]
		if !codex.ValidVersion(*field) {
			return ClientVersions{}, false
		}
	}
	return v, true
}

// CheckResult is check-clients' output: the latest releases and the images rebuilt to them. Latest is absent when
// another check held the lock.
type CheckResult struct {
	Latest  ClientVersions `json:"latest,omitzero"`
	Rebuilt []string       `json:"rebuilt"`
}

// checkState is ~/.ensemble/docker/clients-check.json, written only by check-clients.
type checkState struct {
	CheckedAt time.Time `json:"checked_at"`
	Failure   string    `json:"failure"`
}

// variant is a variant record, images/<key>.json: the build inputs that reproduce key.
type variant struct {
	Package      string   `json:"package"`
	BaseImage    string   `json:"base_image"`
	Plugins      []string `json:"plugins"`
	ImagePlugins []string `json:"image_plugins"`
}

// variantFile matches a key's variant record (json) and replaced list (replaced) in images/.
var variantFile = regexp.MustCompile(`^([0-9a-f]{64})\.(json|replaced)$`)

func (h Host) dockerPath(name string) string {
	return filepath.Join(h.Home, ".ensemble/docker", name)
}

func (h Host) recordPath(key string) string {
	return h.leasePath(key) + ".json"
}

func (h Host) now() time.Time {
	if h.Now != nil {
		return h.Now()
	}
	return time.Now()
}

// writePrivate writes data to path, 0600, through a temporary file in its directory.
func writePrivate(path string, data []byte) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	f, err := os.CreateTemp(dir, "."+filepath.Base(path)+"-")
	if err != nil {
		return err
	}
	_, writeErr := f.Write(data)
	if err := errors.Join(writeErr, f.Close()); err != nil {
		_ = os.Remove(f.Name())
		return err
	}
	if err := os.Rename(f.Name(), path); err != nil {
		_ = os.Remove(f.Name())
		return err
	}
	return nil
}

// record writes key's variant record for check-clients; a failure is only noted.
func (h Host) record(key, pkg string, spec BuildSpec) {
	data, err := json.Marshal(variant{Package: pkg, BaseImage: spec.BaseImage, Plugins: append([]string{}, spec.Plugins...),
		ImagePlugins: append([]string{}, spec.ImagePlugins...)})
	if err == nil {
		err = writePrivate(h.recordPath(key), data)
	}
	if err != nil {
		h.note("cannot record ensemble-worker:" + key + " for the client check: " + err.Error())
	}
}

func (h Host) readCheckState() (checkState, bool) {
	var state checkState
	data, err := os.ReadFile(h.dockerPath("clients-check.json"))
	return state, err == nil && json.Unmarshal(data, &state) == nil
}

func (h Host) writeCheckState(state checkState) error {
	data, err := json.Marshal(state)
	if err != nil {
		return err
	}
	return writePrivate(h.dockerPath("clients-check.json"), data)
}

// afterBuild reports the last check's failure once, then spawns check-clients when one is due; neither fails the build.
func (h Host) afterBuild() {
	state, ok := h.readCheckState()
	if ok {
		marker := h.dockerPath("clients-check.reported")
		reported, _ := os.ReadFile(marker)
		switch {
		case state.Failure != "" && state.Failure != string(reported):
			h.note("client check failed: " + state.Failure + "; starts use the current image")
			_ = writePrivate(marker, []byte(state.Failure))
		case state.Failure == "" && len(reported) > 0:
			_ = writePrivate(marker, nil)
		}
	}
	if h.Spawn != nil && (!ok || h.now().Sub(state.CheckedAt) >= clientCheckInterval) {
		if err := h.Spawn(); err != nil {
			h.note("cannot start the client check: " + err.Error())
		}
	}
}

// checkLock takes the client check lock without waiting; held: another check has it.
func (h Host) checkLock() (unlock func(), held bool, err error) {
	path := h.dockerPath("clients-check.lock")
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, false, err
	}
	f, err := os.OpenFile(path, os.O_RDWR|os.O_CREATE, 0o600)
	if err != nil {
		return nil, false, err
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		_ = f.Close()
		if errors.Is(err, syscall.EWOULDBLOCK) {
			return nil, true, nil
		}
		return nil, false, err
	}
	return func() { _ = f.Close() }, false, nil
}

// CheckClients rebuilds, under its own key, every recorded variant a build used within variantLease whose image's
// clients differ from the latest releases, then prunes. Another check running: it does nothing.
func (h Host) CheckClients(ctx context.Context) (CheckResult, error) {
	unlock, held, err := h.checkLock()
	if err != nil {
		return CheckResult{}, protocol.New(protocol.BuildFailed, "Taking the client check lock ~/.ensemble/docker/clients-check.lock failed.")
	}
	if held {
		return CheckResult{Rebuilt: []string{}}, nil
	}
	defer unlock()
	defer h.prune(ctx, "")
	stateFailed := protocol.New(protocol.BuildFailed, "Cannot write the client check state ~/.ensemble/docker/clients-check.json.")
	state, _ := h.readCheckState()
	state.CheckedAt = h.now()
	if err := h.writeCheckState(state); err != nil {
		return CheckResult{}, stateFailed
	}
	latest, err := h.latestClients(ctx)
	if err != nil {
		state.Failure = "registry: " + err.Error()
		if err := h.writeCheckState(state); err != nil {
			return CheckResult{}, stateFailed
		}
		return CheckResult{}, protocol.New(protocol.BuildFailed, "Cannot look up the latest client releases.")
	}
	result := CheckResult{Latest: latest, Rebuilt: []string{}}
	var failures []string
	entries, _ := os.ReadDir(h.dockerPath("images"))
	for _, entry := range entries {
		m := variantFile.FindStringSubmatch(entry.Name())
		if m == nil || m[2] != "json" {
			continue
		}
		rebuilt, failed := h.checkVariant(ctx, m[1], latest)
		if rebuilt {
			result.Rebuilt = append(result.Rebuilt, "ensemble-worker:"+m[1])
		}
		if failed != "" {
			failures = append(failures, "ensemble-worker:"+m[1]+": "+failed)
		}
	}
	state.Failure = strings.Join(failures, "; ")
	if err := h.writeCheckState(state); err != nil {
		return CheckResult{}, stateFailed
	}
	if state.Failure != "" {
		return result, protocol.New(protocol.BuildFailed, state.Failure)
	}
	return result, nil
}

// checkVariant rebuilds key's variant at latest when its image's clients differ, returning the failure's protocol
// message, never Docker's diagnostic, so a persistent failure keeps one text. A stale record is deleted.
func (h Host) checkVariant(ctx context.Context, key string, latest ClientVersions) (rebuilt bool, failed string) {
	image := "ensemble-worker:" + key
	drop := func(why string) (bool, string) {
		_ = os.Remove(h.recordPath(key))
		h.note("dropped the variant record of " + image + ": " + why)
		return false, ""
	}
	var v variant
	if data, err := os.ReadFile(h.recordPath(key)); err != nil || json.Unmarshal(data, &v) != nil || !filepath.IsAbs(v.Package) {
		return drop("unreadable")
	}
	if !h.inUse(key) {
		return false, ""
	}
	info, found, err := h.inspectImage(ctx, image)
	if err != nil {
		return false, failure(err, protocol.BuildFailed, "Cannot inspect the worker image.").Message
	}
	if !found {
		return drop("no such image")
	}
	if current, ok := imageClients(info.Config.Labels); ok && current == latest {
		return false, ""
	}
	tmp, err := docker.TempDir(h.Home, "ensemble-build-")
	if err != nil {
		return false, "Cannot create a build directory."
	}
	defer os.RemoveAll(tmp)
	p, err := h.prepare(ctx, v.Package, BuildSpec{BaseImage: v.BaseImage, Plugins: v.Plugins, ImagePlugins: v.ImagePlugins}, tmp)
	if err != nil {
		return drop(message(err))
	}
	if p.key != key {
		return drop("its inputs now make another key")
	}
	if _, err := h.rebuild(ctx, tmp, key, p.version, p.dirs, p.spec, p.baseID, latest); err != nil {
		return false, message(err)
	}
	return true, ""
}

// message is err's protocol message.
func message(err error) string {
	var e *protocol.Error
	if errors.As(err, &e) {
		return e.Message
	}
	return "Unexpected broker failure."
}
