package codex

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"sync"
	"syscall"
	"time"
)

const (
	installRoot     = "/usr/local/libexec/ensemble/codex"
	maxInstallation = 1 << 20
)

var ErrNotInstalled = errors.New("pinned Codex is not installed")

var stableVersion = regexp.MustCompile(`^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$`)

func ValidVersion(version string) bool { return stableVersion.MatchString(version) }

type Installation struct {
	Version string `json:"version"`
	Path    string `json:"path"`
	SHA256  string `json:"sha256"`
}

type command struct {
	path, dir string
	args, env []string
}

type Runtime struct {
	root         string
	trustRoot    string
	tempRoot     string
	http         *http.Client
	uid          func() int
	platform     func() (string, string)
	owned        func(os.FileInfo) bool
	execute      func(context.Context, command) ([]byte, error)
	startRefresh func(context.Context, command) (refreshProcess, error)
	rename       func(string, string) error
}

func New() Runtime {
	return Runtime{
		root:         installRoot,
		trustRoot:    "/",
		tempRoot:     "/private/tmp",
		http:         &http.Client{Timeout: 2 * time.Minute},
		uid:          os.Geteuid,
		platform:     func() (string, string) { return runtime.GOOS, runtime.GOARCH },
		owned:        func(fi os.FileInfo) bool { s, ok := fi.Sys().(*syscall.Stat_t); return ok && s.Uid == 0 },
		execute:      execute,
		startRefresh: startRefresh,
		rename:       os.Rename,
	}
}

func execute(ctx context.Context, c command) ([]byte, error) {
	cmd := exec.CommandContext(ctx, c.path, c.args...)
	cmd.WaitDelay = 2 * time.Second
	cmd.Dir, cmd.Env = c.dir, c.env
	cmd.Stderr = io.Discard
	var out boundedOutput
	cmd.Stdout = &out
	err := cmd.Run()
	return out.Bytes(), err
}

type boundedOutput struct{ bytes.Buffer }

func (b *boundedOutput) Write(p []byte) (int, error) {
	n := len(p)
	if b.Len() < 4096 {
		_, _ = b.Buffer.Write(p[:min(len(p), 4096-b.Len())])
	}
	return n, nil
}

func environment(home string) []string {
	return []string{"HOME=" + home, "CODEX_HOME=" + home, "PATH=/usr/bin:/bin:/usr/sbin:/sbin", "LANG=en_US.UTF-8"}
}

func (r Runtime) safePath(name string, directory bool) error {
	if !filepath.IsAbs(name) || filepath.Clean(name) != name {
		return errors.New("Codex path is not canonical")
	}
	parts := []string{name}
	for p := filepath.Dir(name); name != r.trustRoot && p != name; p = filepath.Dir(p) {
		parts = append(parts, p)
		if p == r.trustRoot || p == filepath.Dir(p) {
			break
		}
	}
	for n := len(parts) - 1; n >= 0; n-- {
		fi, err := os.Lstat(parts[n])
		if err != nil {
			return fmt.Errorf("cannot inspect Codex installation: %w", err)
		}
		if fi.Mode()&os.ModeSymlink != 0 || !r.owned(fi) || fi.Mode().Perm()&0022 != 0 || fi.Mode()&(os.ModeSetuid|os.ModeSetgid) != 0 {
			return errors.New("Codex installation ownership or permissions invalid; run sudo ensemble-broker update-codex <version>")
		}
		if (n != 0 || directory) && !fi.IsDir() {
			return errors.New("Codex parent is not a directory")
		}
		if n == 0 && !directory && !fi.Mode().IsRegular() {
			return errors.New("Codex file is not regular")
		}
	}
	return nil
}

func (r Runtime) open(name string) (*os.File, error) {
	if err := r.safePath(name, false); err != nil {
		return nil, err
	}
	fd, err := syscall.Open(name, syscall.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0)
	if err != nil {
		return nil, errors.New("cannot open verified Codex file")
	}
	f := os.NewFile(uintptr(fd), name)
	fi, err := f.Stat()
	current, statErr := os.Lstat(name)
	if err != nil || statErr != nil || !fi.Mode().IsRegular() || !r.owned(fi) || fi.Mode().Perm()&0022 != 0 || !os.SameFile(fi, current) {
		_ = f.Close()
		return nil, errors.New("Codex file changed during verification")
	}
	return f, nil
}

func (r Runtime) metadataRecord() (Installation, error) {
	var i Installation
	f, err := r.open(filepath.Join(r.root, "active.json"))
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return i, ErrNotInstalled
		}
		return i, err
	}
	defer f.Close()
	b, err := io.ReadAll(io.LimitReader(f, maxInstallation+1))
	if err != nil || len(b) > maxInstallation {
		return i, errors.New("Codex metadata exceeds limit")
	}
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&i); err != nil {
		return Installation{}, errors.New("invalid Codex metadata")
	}
	var extra any
	if dec.Decode(&extra) != io.EOF || !stableVersion.MatchString(i.Version) || len(i.SHA256) != 64 {
		return Installation{}, errors.New("invalid Codex metadata")
	}
	if _, err := hex.DecodeString(i.SHA256); err != nil {
		return Installation{}, errors.New("invalid Codex hash")
	}
	parts := strings.Split(filepath.ToSlash(i.Path), "/")
	if len(parts) != 3 || parts[0] != "releases" || parts[1] == "" || parts[1] == "." || parts[1] == ".." || parts[2] != "codex" || filepath.Clean(i.Path) != i.Path {
		return Installation{}, errors.New("invalid Codex artifact path")
	}
	i.Path = filepath.Join(r.root, i.Path)
	return i, nil
}

func (r Runtime) Verify() (Installation, error) {
	i, err := r.metadataRecord()
	if err != nil {
		return Installation{}, err
	}
	native, err := r.open(i.Path)
	if err != nil {
		return Installation{}, err
	}
	defer native.Close()
	fi, err := native.Stat()
	if err != nil || fi.Mode().Perm()&0111 == 0 || fi.Size() > maxExpanded {
		return Installation{}, errors.New("Codex executable mode or size invalid")
	}
	hash := sha256.New()
	if _, err := io.Copy(hash, io.LimitReader(native, maxExpanded+1)); err != nil || hex.EncodeToString(hash.Sum(nil)) != i.SHA256 {
		return Installation{}, errors.New("Codex executable hash mismatch; run sudo ensemble-broker update-codex <version>")
	}
	return i, nil
}

func privateHome(home string) error {
	fi, err := os.Lstat(home)
	if err != nil || !filepath.IsAbs(home) || filepath.Clean(home) != home || !fi.IsDir() || fi.Mode().Perm() != 0700 {
		return errors.New("Codex requires a private temporary home")
	}
	owner, ok := fi.Sys().(*syscall.Stat_t)
	if !ok || owner.Uid != uint32(os.Geteuid()) || fi.Mode()&(os.ModeSetuid|os.ModeSetgid) != 0 {
		return errors.New("Codex temporary home owner invalid")
	}
	return nil
}

func (r Runtime) Run(ctx context.Context, home string, args ...string) error {
	i, err := r.Verify()
	if err != nil {
		return err
	}
	if err := privateHome(home); err != nil {
		return err
	}
	if _, err := r.execute(ctx, command{path: i.Path, dir: home, args: args, env: environment(home)}); err != nil {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		return errors.New("pinned Codex command failed")
	}
	return nil
}

func (r Runtime) lock(ctx context.Context, name string) (func(), error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	f, err := r.open(filepath.Join(r.root, name))
	if err != nil {
		return nil, err
	}
	fi, err := f.Stat()
	if err != nil || fi.Mode().Perm() != 0644 {
		_ = f.Close()
		return nil, errors.New("Codex fixed lock permissions invalid")
	}
	for {
		err = syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
		if err == nil {
			break
		}
		if err != syscall.EWOULDBLOCK && err != syscall.EAGAIN {
			_ = f.Close()
			return nil, errors.New("cannot lock Codex account")
		}
		timer := time.NewTimer(20 * time.Millisecond)
		select {
		case <-ctx.Done():
			timer.Stop()
			_ = f.Close()
			return nil, ctx.Err()
		case <-timer.C:
		}
	}
	var once sync.Once
	return func() { once.Do(func() { _ = syscall.Flock(int(f.Fd()), syscall.LOCK_UN); _ = f.Close() }) }, nil
}

func (r Runtime) Lock(ctx context.Context) (func(), error) { return r.lock(ctx, "auth.lock") }

func (r Runtime) mkdir(name string) error {
	if err := r.safePath(name, true); err == nil {
		return nil
	}
	if _, err := os.Lstat(name); !os.IsNotExist(err) {
		return errors.New("unsafe Codex installation directory")
	}
	parent := filepath.Dir(name)
	if parent == name {
		return errors.New("invalid Codex installation root")
	}
	if err := r.mkdir(parent); err != nil {
		return err
	}
	if err := os.Mkdir(name, 0755); err != nil && !os.IsExist(err) {
		return fmt.Errorf("create Codex directory: %w", err)
	}
	return r.safePath(name, true)
}

func (r Runtime) prepareLock(name string) error {
	path := filepath.Join(r.root, name)
	fd, err := syscall.Open(path, syscall.O_WRONLY|syscall.O_CREAT|syscall.O_EXCL|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0644)
	if err == nil {
		f := os.NewFile(uintptr(fd), path)
		err = f.Chmod(0644)
		_ = f.Close()
		if err != nil {
			return err
		}
	} else if !os.IsExist(err) {
		return errors.New("cannot create Codex fixed lock")
	}
	f, err := r.open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	fi, err := f.Stat()
	if err != nil || fi.Mode().Perm() != 0644 {
		return errors.New("Codex fixed lock permissions invalid")
	}
	return nil
}
