// Package launcher builds worker images and starts and closes their containers.
// Providers read credentials; the launcher delivers their opaque commands.
package launcher

import (
	"context"
	"errors"
	"io"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
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
	Issue(ctx context.Context, app, repo string, permissions map[string]string, container string) ([]credential.Command, func(), []protocol.Expiry, error)
	Refresh(ctx context.Context, app, repo string, permissions map[string]string, container string, exec func(argv []string, stdin []byte) error) ([]protocol.Expiry, error)
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

type Host struct {
	Home     string
	Endpoint string
	IsDir    func(string) bool
	Safe     func(string) error
	Docker   docker.Runner
	Creds    Credentials
	Codex    CodexCredentials
	Opencode ProviderKeys
	Env      EnvSecrets
	Static   StaticMeta // nil: no static secret records, as tests without them
	Schedule Schedule   // nil: no token record, as tests without one; every token provider is then always due
	GitHub   Tokens
	Linear   LinearTokens
	Versions CodexVersions
	Releases Releases                          // nil: a build that must rebuild fails
	Progress io.Writer                         // nil: none; build's notes on older images, after Docker's progress; start's phase times
	Lock     func() (unlock func(), err error) // the image lock: build's lease and inspect, and prune
	Audit    *audit.Recorder                   // nil: none; start, maintain and close name their container and add the client credential
	Now      func() time.Time                  // nil: host clock; mutex expiry, replaced image and check state tests supply a clock
	Spawn    func() error                      // nil: none; starts check-clients detached, for a build
}

// NewHost wires the real filesystem; tests build Host with fakes. rec is the command's audit recorder, the one tokens
// also reports to.
func NewHost(home, endpoint string, d docker.Runner, creds Credentials, tokens Tokens, rec *audit.Recorder) Host {
	return Host{
		Home:     home,
		Endpoint: endpoint,
		Creds:    creds,
		GitHub:   tokens,
		Audit:    rec,
		IsDir:    isDir,
		Safe:     realSafe,
		Schedule: StateSchedule{Home: home, Safe: realSafe},
		Docker:   d,
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
	Container    string    `json:"container"`
	Endpoint     string    `json:"endpoint"`
	ImageDigest  string    `json:"image_digest"`
	Clients      Clients   `json:"clients"`
	GitHubUserID *int64    `json:"github_user_id"`
	Deadline     *Deadline `json:"deadline"`
}

type StatusResult struct {
	Codex  *protocol.CodexStatus `json:"codex,omitempty"`
	Claude map[string]bool       `json:"claude,omitempty"`
	Static []StaticStatus        `json:"static,omitempty"`
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

// failure classifies a Docker error: unreachable daemon, else code.
func failure(err error, code protocol.Code, message string) *protocol.Error {
	if errors.Is(err, docker.ErrUnreachable) {
		return protocol.New(protocol.DockerUnreachable, "Cannot reach Docker Desktop; start it and retry.")
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
