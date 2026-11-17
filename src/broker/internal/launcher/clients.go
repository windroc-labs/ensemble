package launcher

import (
	"bytes"
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

// checkState is a daemon's clients-check.json in its image records, written only by check-clients.
type checkState struct {
	CheckedAt time.Time `json:"checked_at"`
	Failure   string    `json:"failure"`
}

// variant is a variant record, <key>.json in a daemon's image records: the build inputs that reproduce key.
type variant struct {
	Package      string   `json:"package"`
	BaseImage    string   `json:"base_image"`
	Plugins      []string `json:"plugins"`
	ImagePlugins []string `json:"image_plugins"`
}

// variantFile matches a key's variant record (json) and replaced list (replaced) in a daemon's image records.
var variantFile = regexp.MustCompile(`^([0-9a-f]{64})\.(json|replaced)$`)

// daemonPattern is a usable Docker daemon ID: docker info prints untrusted text, and the ID names a Mac directory.
var daemonPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$`)

func (h Host) dockerPath(name string) string {
	return filepath.Join(h.Home, ".ensemble/docker", name)
}

// records is an identified daemon's image records dir, images/<ID>: an image exists only on the daemon that built it.
type records struct{ dir string }

func (r records) path(name string) string { return filepath.Join(r.dir, name) }

func (r records) leasePath(key string) string { return r.path(key) }

func (r records) recordPath(key string) string { return r.path(key + ".json") }

func (r records) replacedPath(key string) string { return r.path(key + ".replaced") }

// identify is d with its ID, and its image records; failures are code, an unreachable daemon docker_unreachable.
func (h Host) identify(ctx context.Context, d Daemon, code protocol.Code) (Daemon, records, error) {
	id, err := daemonID(ctx, d)
	var u *docker.Unreachable
	switch {
	case errors.As(err, &u):
		return Daemon{}, records{}, failure(err, code, "")
	case err != nil:
		return Daemon{}, records{}, protocol.New(code, why(err))
	}
	d.ID = id
	return d, records{filepath.Join(h.dockerPath("images"), id)}, nil
}

// why is a failure's message: a protocol error's own, else its text.
func why(err error) string {
	if e, ok := errors.AsType[*protocol.Error](err); ok {
		return e.Message
	}
	return err.Error()
}

// daemonID is d's daemon's ID: a *docker.Unreachable when d cannot be reached, else a *protocol.Error whose message
// says why.
func daemonID(ctx context.Context, d Daemon) (string, error) {
	out, err := d.Runner.Run(ctx, docker.InfoID, nil)
	var u *docker.Unreachable
	switch {
	case errors.As(err, &u):
		return "", u
	case err != nil:
		return "", protocol.New(protocol.InternalError, "Cannot read the Docker daemon's ID.")
	}
	var id string
	if json.Unmarshal(bytes.TrimSpace(out), &id) != nil || !daemonPattern.MatchString(id) {
		return "", protocol.Newf(protocol.InternalError, "The Docker daemon's ID %.80q is not usable.", strings.TrimSpace(string(out)))
	}
	return id, nil
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
func (h Host) record(r records, key, pkg string, spec BuildSpec) {
	data, err := json.Marshal(variant{Package: pkg, BaseImage: spec.BaseImage, Plugins: append([]string{}, spec.Plugins...),
		ImagePlugins: append([]string{}, spec.ImagePlugins...)})
	if err == nil {
		err = writePrivate(r.recordPath(key), data)
	}
	if err != nil {
		h.note("cannot record ensemble-worker:" + key + " for the client check: " + err.Error())
	}
}

func readCheckState(r records) (checkState, bool) {
	var state checkState
	data, err := os.ReadFile(r.path("clients-check.json"))
	return state, err == nil && json.Unmarshal(data, &state) == nil
}

func writeCheckState(r records, state checkState) error {
	data, err := json.Marshal(state)
	if err != nil {
		return err
	}
	return writePrivate(r.path("clients-check.json"), data)
}

// afterBuild reports the last check's failure once, then spawns check-clients when one is due; neither fails the build.
func (h Host) afterBuild(r records) {
	state, ok := readCheckState(r)
	if ok {
		marker := r.path("clients-check.reported")
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
// clients differ from the latest releases, then prunes, on each configured daemon in turn. A daemon's failure does not
// stop the others' checks; the first failure is the result's error. Another check running: it does nothing.
func (h Host) CheckClients(ctx context.Context) (CheckResult, error) {
	unlock, held, err := h.checkLock()
	if err != nil {
		return CheckResult{}, protocol.New(protocol.BuildFailed, "Taking the client check lock ~/.ensemble/docker/clients-check.lock failed.")
	}
	if held {
		return CheckResult{Rebuilt: []string{}}, nil
	}
	defer unlock()
	result := CheckResult{Rebuilt: []string{}}
	var first error
	for _, d := range h.Daemons {
		checked, err := h.checkDaemon(ctx, d)
		if result.Latest == (ClientVersions{}) {
			result.Latest = checked.Latest
		}
		result.Rebuilt = append(result.Rebuilt, checked.Rebuilt...)
		if first == nil {
			first = err
		}
	}
	return result, first
}

// checkDaemon is CheckClients on d alone.
func (h Host) checkDaemon(ctx context.Context, d Daemon) (CheckResult, error) {
	d, r, err := h.identify(ctx, d, protocol.BuildFailed)
	if err != nil {
		return CheckResult{}, err
	}
	defer h.prune(ctx, d, r, "")
	stateFailed := protocol.New(protocol.BuildFailed, "Cannot write the client check state ~/.ensemble/docker/images/"+d.ID+"/clients-check.json.")
	state, _ := readCheckState(r)
	state.CheckedAt = h.now()
	if err := writeCheckState(r, state); err != nil {
		return CheckResult{}, stateFailed
	}
	latest, err := h.latestClients(ctx)
	if err != nil {
		state.Failure = "registry: " + err.Error()
		if err := writeCheckState(r, state); err != nil {
			return CheckResult{}, stateFailed
		}
		return CheckResult{}, protocol.New(protocol.BuildFailed, "Cannot look up the latest client releases.")
	}
	result := CheckResult{Latest: latest, Rebuilt: []string{}}
	var failures []string
	entries, _ := os.ReadDir(r.dir)
	for _, entry := range entries {
		m := variantFile.FindStringSubmatch(entry.Name())
		if m == nil || m[2] != "json" {
			continue
		}
		rebuilt, failed := h.checkVariant(ctx, d, r, m[1], latest)
		if rebuilt {
			result.Rebuilt = append(result.Rebuilt, "ensemble-worker:"+m[1])
		}
		if failed != "" {
			failures = append(failures, "ensemble-worker:"+m[1]+": "+failed)
		}
	}
	state.Failure = strings.Join(failures, "; ")
	if err := writeCheckState(r, state); err != nil {
		return CheckResult{}, stateFailed
	}
	if state.Failure != "" {
		return result, protocol.New(protocol.BuildFailed, state.Failure)
	}
	return result, nil
}

// checkVariant rebuilds key's variant at latest when its image's clients differ, returning the failure's protocol
// message, never Docker's diagnostic, so a persistent failure keeps one text. A stale record is deleted.
func (h Host) checkVariant(ctx context.Context, d Daemon, r records, key string, latest ClientVersions) (rebuilt bool, failed string) {
	image := "ensemble-worker:" + key
	drop := func(why string) (bool, string) {
		_ = os.Remove(r.recordPath(key))
		h.note("dropped the variant record of " + image + ": " + why)
		return false, ""
	}
	var v variant
	if data, err := os.ReadFile(r.recordPath(key)); err != nil || json.Unmarshal(data, &v) != nil || !filepath.IsAbs(v.Package) {
		return drop("unreadable")
	}
	if !h.inUse(r, key) {
		return false, ""
	}
	info, found, err := h.inspectImage(ctx, d, image)
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
	p, err := h.prepare(ctx, d, v.Package, BuildSpec{BaseImage: v.BaseImage, Plugins: v.Plugins, ImagePlugins: v.ImagePlugins}, tmp)
	if err != nil {
		return drop(message(err))
	}
	if p.key != key {
		return drop("its inputs now make another key")
	}
	if _, err := h.rebuild(ctx, d, r, tmp, key, p.version, p.dirs, p.spec, p.baseID, latest); err != nil {
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
