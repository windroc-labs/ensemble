// Package launcher builds worker images and starts and closes their containers.
package launcher

import (
	"cmp"
	"context"
	"errors"
	"io"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"syscall"
	"time"

	"ensemble/broker/internal/audit"
	"ensemble/broker/internal/codex"
	"ensemble/broker/internal/credential"
	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/protocol"
	"ensemble/broker/internal/releases"
)

// Credentials: the caller executes and erases the command.
type Credentials interface {
	Ready(account string) error
	Inject(account string) (credential.Command, error)
}

// CodexInjection holds the account lock until Finish.
type CodexInjection struct {
	Command credential.Command
	Expiry  string
	Finish  func() error
}

type CodexCredentials interface {
	Ready(context.Context) error
	Inject(context.Context) (CodexInjection, error)
	Refresh(ctx context.Context, exec func([]string, []byte) error, publish func(string) error) (string, error)
}

type ProviderKeys interface {
	Ready(providers []string) error
	Inject(providers []string) ([]credential.Command, error)
}

type EnvSecrets interface {
	Ready(items []string) error
	Inject(s Secret) (credential.Command, error)
}

// Tokens' errors never carry credential values.
type Tokens interface {
	Ready(app string) error
	BotUserID(app string) (int64, error)
	// Issue revokes all issued tokens on failure; on success the caller owns revoke, for a later startup failure.
	Issue(ctx context.Context, app string, permissions map[string]string, container string) ([]credential.Command, func(), []protocol.Expiry, error)
	Refresh(ctx context.Context, app string, permissions map[string]string, container string, exec func(argv []string, stdin []byte) error) ([]protocol.Expiry, error)
	Revoke(ctx context.Context, container string, read func(ctx context.Context, argv []string) ([]byte, error))
	Prefetch(ctx context.Context, repo, ref, dir, container string, run func(argv []string, stdin []byte) error) error
}

// LinearTokens delivers a container token, never the app's host token.
type LinearTokens interface {
	Ready(credential.LinearApp) error
	Issue(context.Context, credential.LinearRequest, string) ([]credential.Command, func(), string, error)
	Refresh(context.Context, credential.LinearRequest, string, func([]string, []byte) error) (string, error)
	Revoke(context.Context, string) error
}

const (
	DaemonLocal  = "local"
	DaemonRemote = "remote"
)

type Daemon struct {
	Name     string
	ID       string
	Endpoint string
	Remote   bool
	Path     string
	Runner   docker.Runner
}

type TargetDaemon struct {
	Name        string `json:"name"`
	Endpoint    string `json:"endpoint"`
	Remote      bool   `json:"remote"`
	Path        string `json:"path"`
	ID          string `json:"id,omitempty"`
	Unreachable string `json:"unreachable,omitempty"`
}

type TargetResult struct {
	Default string         `json:"default"`
	Daemons []TargetDaemon `json:"daemons"`
}

func (h Host) Target(ctx context.Context) TargetResult {
	result := TargetResult{Default: h.Daemons[0].Name, Daemons: []TargetDaemon{}}
	for _, d := range h.Daemons {
		t := TargetDaemon{Name: d.Name, Endpoint: d.Endpoint, Remote: d.Remote, Path: d.Path}
		id, err := daemonID(ctx, d)
		var u *docker.Unreachable
		switch {
		case err == nil:
			t.ID = id
		case errors.As(err, &u):
			t.Unreachable = u.Reason
		default:
			t.Unreachable = why(err)
		}
		result.Daemons = append(result.Daemons, t)
	}
	return result
}

type Host struct {
	Home      string
	IsDir     func(string) bool
	Safe      func(string) error
	Daemons   []Daemon // the default first; never empty
	LocalOnly []string
	Creds     Credentials
	Codex     CodexCredentials
	Opencode  ProviderKeys
	Env       EnvSecrets
	Schedule  Schedule // nil: every token provider is always due
	GitHub    Tokens
	Linear    LinearTokens
	Versions  CodexVersions
	Releases  Releases // nil: a build that must rebuild fails
	Progress  io.Writer
	Lock      func() (unlock func(), err error) // orders build's lease and inspect against prune
	Audit     *audit.Recorder
	Now       func() time.Time
	Spawn     func() error
}

func ValidDaemon(id string) bool { return daemonPattern.MatchString(id) }

// find asks the local daemon first, so a local ID opens no ssh connection.
func (h Host) find(ctx context.Context, id string, code protocol.Code) (Daemon, records, error) {
	isRemote := func(d Daemon) bool { return d.Remote }
	isLocal := func(d Daemon) bool { return !d.Remote }
	order := slices.Concat(slices.DeleteFunc(slices.Clone(h.Daemons), isRemote), slices.DeleteFunc(slices.Clone(h.Daemons), isLocal))
	var failed error
	configured := make([]string, 0, len(order))
	for _, d := range order {
		found, r, err := h.identify(ctx, d, code)
		switch {
		case err == nil && found.ID == id:
			return found, r, nil
		case err == nil:
			configured = append(configured, d.Name+" "+d.Endpoint+" ("+found.ID+")")
		default:
			failed = cmp.Or(failed, err)
		}
	}
	if failed != nil {
		return Daemon{}, records{}, failed
	}
	return Daemon{}, records{}, protocol.Newf(protocol.DockerUnreachable, "Docker daemon %s is not configured; the configured daemons: %s.",
		id, strings.Join(configured, ", "))
}

func (h Host) Find(ctx context.Context, id string, code protocol.Code) (Daemon, error) {
	d, _, err := h.find(ctx, id, code)
	return d, err
}

// localOnly trusts only d.Remote, never the manifest's naming of the daemon.
func (h Host) localOnly(d Daemon, m Manifest) error {
	if !d.Remote {
		return nil
	}
	for _, s := range m.Secrets {
		if slices.Contains(h.LocalOnly, s.Item) {
			return protocol.Newf(protocol.ManifestRejected,
				"Manifest rejected: environment secret %s stays on the local Docker Desktop (local_secrets in ~/%s); this worker's daemon is %s %s.",
				s.Item, docker.TargetFile, d.Name, d.Endpoint)
		}
	}
	return nil
}

func secretItems(secrets []Secret) []string {
	items := make([]string, len(secrets))
	for i, s := range secrets {
		items[i] = s.Item
	}
	return items
}

func NewHost(home string, daemons []Daemon, creds Credentials, tokens Tokens, rec *audit.Recorder) Host {
	return Host{
		Home:     home,
		Daemons:  daemons,
		Creds:    creds,
		GitHub:   tokens,
		Audit:    rec,
		IsDir:    isDir,
		Safe:     realSafe,
		Schedule: StateSchedule{Home: home, Safe: realSafe},
		Versions: codex.New(),
		Releases: releases.New(),
		Spawn:    func() error { return spawnCheck(home) },
	}
}

const checkLogMax = 1 << 20

func spawnCheck(home string) error {
	self, err := os.Executable()
	if err != nil {
		return err
	}
	dir := filepath.Join(home, ".ensemble/docker")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	log, err := openCheckLog(dir, os.Rename)
	if err != nil {
		return err
	}
	defer log.Close()
	cmd := exec.Command(self, "check-clients")
	cmd.Stdout, cmd.Stderr = log, log
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	if err := cmd.Start(); err != nil {
		return err
	}
	return cmd.Process.Release()
}

// openCheckLog rotates by rename: a running check keeps writing to the moved file, so no output is lost.
func openCheckLog(dir string, rename func(oldpath, newpath string) error) (*os.File, error) {
	path := filepath.Join(dir, "clients-check.log")
	if info, err := os.Stat(path); err == nil && info.Size() > checkLogMax {
		if err := rename(path, path+".1"); err != nil && !errors.Is(err, fs.ErrNotExist) {
			return nil, err
		}
	}
	return os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
}

type BuildResult struct {
	Image   string `json:"image"`
	Digest  string `json:"digest"`
	Rebuilt bool   `json:"rebuilt"`
}

type Clients struct {
	Claude   string `json:"claude"`
	Codex    string `json:"codex"`
	Opencode string `json:"opencode"`
}

type StartResult struct {
	Container   string            `json:"container"`
	Endpoint    string            `json:"endpoint"`
	Daemon      string            `json:"daemon"`
	ImageDigest string            `json:"image_digest"`
	Clients     Clients           `json:"clients"`
	Env         map[string]string `json:"env"`
	Deadline    *Deadline         `json:"deadline"`
}

type StatusResult struct {
	Codex  *protocol.CodexStatus `json:"codex,omitempty"`
	Claude map[string]bool       `json:"claude"` // whether each account's setup-token is usable
}

type CloseResult struct {
	Container string `json:"container"`
	Removed   bool   `json:"removed"`
}

type WorkspaceResult struct {
	Volume  string `json:"volume"`
	Removed bool   `json:"removed"`
}

// secretsLabel holds secret references, never a value; manifest labels cannot use the ensemble. prefix.
const (
	clientLabel  = "ensemble.client"
	secretsLabel = "ensemble.secrets"
)

func clientKind(client string) string {
	switch client {
	case "claude":
		return audit.ClaudeSetupToken
	case "codex":
		return audit.CodexAccessToken
	}
	return ""
}

func failure(err error, code protocol.Code, message string) *protocol.Error {
	var u *docker.Unreachable
	if errors.As(err, &u) {
		return protocol.New(protocol.DockerUnreachable, u.Error())
	}
	return protocol.New(code, message)
}

// diagnosed adds Docker's diagnostic only for the steps the protocol names.
func diagnosed(err error, code protocol.Code, message string) *protocol.Error {
	e := failure(err, code, message)
	if e.Code == code {
		e.Cause = docker.Diagnostic(err)
	}
	return e
}
