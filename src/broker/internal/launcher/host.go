// Package launcher builds worker images and starts and closes their containers.
// Providers read credentials; the launcher delivers their opaque commands.
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

// Credentials prepares the selected Claude token; the caller executes and erases the command.
type Credentials interface {
	Ready(account string) error
	Inject(account string) (credential.Command, error)
}

// CodexInjection holds the account lock until Finish, including actual delivery and expiry publication.
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

// ProviderKeys transfers owned provider-key commands to the caller.
type ProviderKeys interface {
	Ready(providers []string) error
	Inject(providers []string) ([]credential.Command, error)
}

type EnvSecrets interface {
	Ready(items []string) error
	Inject(s Secret) (credential.Command, error)
}

// Tokens prepares startup writes; refresh remains synchronous. Errors never carry credential values.
type Tokens interface {
	Ready(app string) error
	BotUserID(app string) (int64, error)
	// Issue revokes all issued tokens on preparation failure. On success the caller owns
	// commands and revoke, which revokes every issued token once if startup later fails.
	Issue(ctx context.Context, app string, permissions map[string]string, container string) ([]credential.Command, func(), []protocol.Expiry, error)
	Refresh(ctx context.Context, app string, permissions map[string]string, container string, exec func(argv []string, stdin []byte) error) ([]protocol.Expiry, error)
	Revoke(ctx context.Context, container string, read func(ctx context.Context, argv []string) ([]byte, error))
	Prefetch(ctx context.Context, repo, ref, dir, container string, run func(argv []string, stdin []byte) error) error
}

// LinearTokens delivers a role app's separate container token, never its host token.
type LinearTokens interface {
	Ready(credential.LinearApp) error
	Issue(context.Context, credential.LinearRequest, string) ([]credential.Command, func(), string, error)
	Refresh(context.Context, credential.LinearRequest, string, func([]string, []byte) error) (string, error)
	Revoke(context.Context, string) error
}

// The configured daemons' names: the local Docker Desktop, always present, and the remote target, when set.
const (
	DaemonLocal  = "local"
	DaemonRemote = "remote"
)

// Daemon is one configured Docker daemon, built once per command; ID is set only by identify.
type Daemon struct {
	Name     string
	ID       string
	Endpoint string
	Remote   bool
	Path     string // the PATH its docker runs with
	Runner   docker.Runner
}

// TargetDaemon is one configured daemon as the target command reports it: its ID once reached, else why not.
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

// Target is every configured daemon, the default first, each identified once.
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
	Daemons   []Daemon // every configured daemon, the default first; never empty
	LocalOnly []string // environment secret items no remote daemon is given
	Creds     Credentials
	Codex     CodexCredentials
	Opencode  ProviderKeys
	Env       EnvSecrets
	Schedule  Schedule // nil: no token record, as tests without one; every token provider is then always due
	GitHub    Tokens
	Linear    LinearTokens
	Versions  CodexVersions
	Releases  Releases                          // nil: a build that must rebuild fails
	Progress  io.Writer                         // nil: none; build's notes on older images, after Docker's progress; start's phase times
	Lock      func() (unlock func(), err error) // the image lock: build's lease and inspect, and prune
	Audit     *audit.Recorder                   // nil: none; start, maintain and close name their container and add the client credential
	Now       func() time.Time                  // nil: host clock; replaced image and check state tests supply a clock
	Spawn     func() error                      // nil: none; starts check-clients detached, for a build
}

// ValidDaemon is whether id can name a Docker daemon.
func ValidDaemon(id string) bool { return daemonPattern.MatchString(id) }

// find is the configured daemon whose ID is id, identified, and its image records; the local Docker Desktop is asked
// first, so a local ID opens no ssh connection. With no daemon of that ID: the first daemon that could not be
// identified fails it (an unreachable one docker_unreachable, else code), else docker_unreachable names id and every
// configured daemon.
func (h Host) find(ctx context.Context, id string, code protocol.Code) (Daemon, records, error) {
	order := slices.Clone(h.Daemons)
	slices.SortStableFunc(order, func(a, b Daemon) int {
		switch {
		case a.Remote == b.Remote:
			return 0
		case a.Remote:
			return 1
		}
		return -1
	})
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

// Find is the configured daemon whose ID is id, by find's rule.
func (h Host) Find(ctx context.Context, id string, code protocol.Code) (Daemon, error) {
	d, _, err := h.find(ctx, id, code)
	return d, err
}

// localOnly refuses a start that would give a remote daemon d any of the environment secrets that stay on the local
// Docker Desktop: the manifest names the daemon, so this rule never trusts its naming, only d.Remote.
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

// secretItems are secrets' keychain items.
func secretItems(secrets []Secret) []string {
	items := make([]string, len(secrets))
	for i, s := range secrets {
		items[i] = s.Item
	}
	return items
}

// NewHost wires the real filesystem; tests build Host with fakes. rec is the command's audit recorder, the one tokens
// also reports to.
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

// spawnCheck starts this executable's check-clients in a session of its own, outliving the build, its output in
// ~/.ensemble/docker/clients-check.log.
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

// openCheckLog opens dir's clients-check.log for appending, first moving one past checkLogMax to clients-check.log.1:
// a running check keeps writing to the moved file, so a spawn never loses its output. A log another spawn moved first
// is fine.
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
	Claude map[string]bool       `json:"claude"` // each account logged in by name: whether its setup-token is usable
}

type CloseResult struct {
	Container string `json:"container"`
	Removed   bool   `json:"removed"`
}

type WorkspaceResult struct {
	Volume  string `json:"volume"`
	Removed bool   `json:"removed"`
}

// clientLabel names the client a container was started for, secretsLabel the environment secrets it got (their
// references, comma-joined; never a value); manifest labels cannot use the ensemble. prefix.
const (
	clientLabel  = "ensemble.client"
	secretsLabel = "ensemble.secrets"
)

// clientKind is the audit kind of the credential client's container holds; "" for none.
func clientKind(client string) string {
	switch client {
	case "claude":
		return audit.ClaudeSetupToken
	case "codex":
		return audit.CodexAccessToken
	}
	return ""
}

// failure classifies a Docker error: an unreachable target, named with its reason, else code.
func failure(err error, code protocol.Code, message string) *protocol.Error {
	var u *docker.Unreachable
	if errors.As(err, &u) {
		return protocol.New(protocol.DockerUnreachable, u.Error())
	}
	return protocol.New(code, message)
}

// diagnosed is failure with Docker's diagnostic as cause, only for the steps the protocol names: build stages, run, and workspace-rm's check and volume rm.
func diagnosed(err error, code protocol.Code, message string) *protocol.Error {
	e := failure(err, code, message)
	if e.Code == code {
		e.Cause = docker.Diagnostic(err)
	}
	return e
}
