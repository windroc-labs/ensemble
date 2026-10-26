package codex

import (
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/sha512"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"

	"ensemble/broker/internal/npm"
)

const (
	maxArchive  = 256 << 20
	maxExpanded = 512 << 20
)

type packageMetadata struct {
	Name     string            `json:"name"`
	Version  string            `json:"version"`
	OS       []string          `json:"os"`
	CPU      []string          `json:"cpu"`
	Optional map[string]string `json:"optionalDependencies"`
	Dist     struct {
		Tarball   string `json:"tarball"`
		Integrity string `json:"integrity"`
	} `json:"dist"`
}

func (r Runtime) metadata(ctx context.Context, name, version string) (packageMetadata, error) {
	var m packageMetadata
	b, err := npm.Fetch(ctx, r.http, "Codex", npm.Package(name, version), npm.MaxMetadata)
	if err != nil {
		return m, err
	}
	if err := json.Unmarshal(b, &m); err != nil || m.Name != name || m.Version != version || !npm.Allowed(m.Dist.Tarball) {
		return packageMetadata{}, errors.New("Codex package metadata rejected")
	}
	return m, nil
}

func (r Runtime) packageEntry(ctx context.Context, m packageMetadata, entry string) ([]byte, error) {
	b, err := r.packageArchive(ctx, m)
	if err != nil {
		return nil, err
	}
	return readEntryBounded(b, entry, maxExpanded)
}

func (r Runtime) packageArchive(ctx context.Context, m packageMetadata) ([]byte, error) {
	if !strings.HasPrefix(m.Dist.Integrity, "sha512-") {
		return nil, errors.New("Codex package requires SHA-512 integrity")
	}
	want, err := base64.StdEncoding.DecodeString(strings.TrimPrefix(m.Dist.Integrity, "sha512-"))
	if err != nil || len(want) != sha512.Size {
		return nil, errors.New("Codex integrity malformed")
	}
	b, err := npm.Fetch(ctx, r.http, "Codex", m.Dist.Tarball, maxArchive)
	if err != nil {
		return nil, err
	}
	got := sha512.Sum512(b)
	if !bytes.Equal(got[:], want) {
		return nil, errors.New("Codex package integrity mismatch")
	}
	return b, nil
}

func (r Runtime) platformEntry(ctx context.Context, version, goos, cpu, triple string) ([]byte, error) {
	b, err := r.platformArchive(ctx, version, goos, cpu)
	if err != nil {
		return nil, err
	}
	return readEntryBounded(b, "package/vendor/"+triple+"/bin/codex", maxExpanded)
}

func (r Runtime) platformArchive(ctx context.Context, version, goos, cpu string) ([]byte, error) {
	main, err := r.metadata(ctx, "@openai/codex", version)
	if err != nil {
		return nil, err
	}
	dependency, ok := main.Optional["@openai/codex-"+goos+"-"+cpu]
	if !ok {
		return nil, errors.New("Codex platform dependency missing")
	}
	name, platformVersion := "@openai/codex-"+goos+"-"+cpu, version
	if strings.HasPrefix(dependency, "npm:") {
		if dependency != "npm:@openai/codex@"+version+"-"+goos+"-"+cpu {
			return nil, errors.New("Codex platform alias rejected")
		}
		name, platformVersion = "@openai/codex", version+"-"+goos+"-"+cpu
	} else if dependency != version {
		return nil, errors.New("Codex platform version rejected")
	}
	if _, err := r.packageEntry(ctx, main, ""); err != nil {
		return nil, err
	}
	platform, err := r.metadata(ctx, name, platformVersion)
	if err != nil {
		return nil, err
	}
	if len(platform.OS) != 1 || platform.OS[0] != goos || len(platform.CPU) != 1 || platform.CPU[0] != cpu {
		return nil, errors.New("Codex platform package target rejected")
	}
	return r.packageArchive(ctx, platform)
}

func (r Runtime) Install(ctx context.Context, version string, update bool) (Installation, error) {
	var empty Installation
	if !stableVersion.MatchString(version) {
		return empty, errors.New("Codex requires an exact stable version")
	}
	if !SupportedVersion(version) {
		return empty, errors.New("Codex requires version " + MinVersion + " or newer")
	}
	if r.uid() != 0 {
		return empty, errors.New("Codex installation requires sudo")
	}
	goos, goarch := r.platform()
	if goos != "darwin" || goarch != "arm64" {
		return empty, errors.New("Codex installation requires macOS arm64")
	}
	if err := r.mkdir(r.root); err != nil {
		return empty, err
	}
	if err := r.prepareLock("install.lock"); err != nil {
		return empty, err
	}
	unlockInstall, err := r.lock(ctx, "install.lock")
	if err != nil {
		return empty, err
	}
	defer unlockInstall()
	if err := r.prepareLock("auth.lock"); err != nil {
		return empty, err
	}
	_, verifyErr := r.metadataRecord()
	_, statErr := os.Lstat(filepath.Join(r.root, "active.json"))
	exists := !os.IsNotExist(statErr)
	if !update && exists {
		return empty, errors.New("Codex already installed; use update-codex <version>")
	}
	if update && !exists {
		return empty, errors.New("Codex is not installed; use install-codex <version>")
	}
	if update && verifyErr != nil {
		return empty, verifyErr
	}
	native, err := r.platformEntry(ctx, version, "darwin", "arm64", "aarch64-apple-darwin")
	if err != nil {
		return empty, err
	}

	releases := filepath.Join(r.root, "releases")
	if err := r.mkdir(releases); err != nil {
		return empty, err
	}
	stage, err := os.MkdirTemp(releases, "release-")
	if err != nil {
		return empty, errors.New("cannot stage Codex release")
	}
	published := false
	defer func() {
		if !published {
			_ = os.RemoveAll(stage)
		}
	}()
	if err := os.Chmod(stage, 0755); err != nil {
		return empty, err
	}
	nativePath := filepath.Join(stage, "codex")
	if err := writeFile(nativePath, native, 0755); err != nil {
		return empty, err
	}
	if err := r.safePath(nativePath, false); err != nil {
		return empty, err
	}
	probeHome, err := os.MkdirTemp(r.tempRoot, "ensemble-codex-probe-")
	if err != nil {
		return empty, errors.New("cannot create private Codex probe home")
	}
	defer os.RemoveAll(probeHome)
	probe, err := r.execute(ctx, command{path: nativePath, dir: probeHome, args: []string{"--version"}, env: environment(probeHome)})
	if err != nil || strings.TrimSpace(string(probe)) != "codex-cli "+version {
		return empty, errors.New("Codex native version verification failed")
	}
	sum := sha256.Sum256(native)
	i := Installation{Version: version, Path: filepath.Join("releases", filepath.Base(stage), "codex"), SHA256: hex.EncodeToString(sum[:])}
	metadata, err := json.Marshal(i)
	if err != nil {
		return empty, err
	}
	tmp, err := os.CreateTemp(r.root, ".active-")
	if err != nil {
		return empty, errors.New("cannot stage Codex metadata")
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName)
	if _, err = tmp.Write(metadata); err == nil {
		err = tmp.Chmod(0644)
	}
	if err == nil {
		err = tmp.Sync()
	}
	closeErr := tmp.Close()
	if err != nil || closeErr != nil {
		return empty, errors.New("cannot write Codex metadata")
	}
	unlockAuth, err := r.Lock(ctx)
	if err != nil {
		return empty, err
	}
	defer unlockAuth()
	if err := ctx.Err(); err != nil {
		return empty, err
	}
	if err := r.rename(tmpName, filepath.Join(r.root, "active.json")); err != nil {
		return empty, errors.New("cannot publish Codex installation")
	}
	published = true
	i.Path = nativePath
	return i, nil
}

func writeFile(path string, b []byte, mode os.FileMode) error {
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, mode)
	if err != nil {
		return errors.New("cannot stage Codex executable")
	}
	_, err = f.Write(b)
	if err == nil {
		err = f.Chmod(mode)
	}
	if err == nil {
		err = f.Sync()
	}
	closeErr := f.Close()
	if err != nil || closeErr != nil {
		return errors.New("cannot write Codex executable")
	}
	return nil
}
