// Package launcher builds worker images and starts and closes their containers.
// It never touches the keychain or a token: injection goes through Credentials and Tokens.
package launcher

import (
	"context"
	"errors"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"time"

	"ensemble/broker/internal/audit"
	"ensemble/broker/internal/codex"
	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/protocol"
	"ensemble/broker/internal/releases"
)

// Credentials is the broker package's side of start: each Claude account's setup-token, which the launcher never sees.
type Credentials interface {
	Ready(account string) error // *protocol.Error: login_required or keychain_failed
	// Inject writes account's token through exec, which runs argv as agent in the container being started.
	// Its errors are *protocol.Error (login_required, keychain_failed), wrap docker.ErrUnreachable
	// or are opaque, carrying no Docker stderr.
	Inject(account string, exec func(argv []string, stdin []byte) error) error
}

// CodexCredentials is the broker package's side of a Codex start; the launcher never sees the login.
type CodexCredentials interface {
	Ready(context.Context) error
	// Inject writes the login through exec, as Credentials.Inject does, and returns its expiry (RFC 3339).
	Inject(context.Context, func([]string, []byte) error) (string, error)
	Refresh(context.Context, func([]string, []byte) error, func(string) error) (string, error)
}

// ProviderKeys is the broker package's side of an OpenCode start: the listed providers' API keys, which the launcher
// never sees. Errors are as Credentials'.
type ProviderKeys interface {
	Ready(providers []string) error
	// Inject writes each provider's key through exec, as Credentials.Inject does.
	Inject(providers []string, exec func(argv []string, stdin []byte) error) error
}

// EnvSecrets is the broker package's side of a start's environment secrets, which the launcher never sees: each item
// is a reference, ensemble-env/<name>. Errors are as Credentials'.
type EnvSecrets interface {
	Ready(items []string) error
	// Inject writes one secret's value through exec, as Credentials.Inject does, to the file s.File.
	Inject(s Secret, exec func(argv []string, stdin []byte) error) error
}

// Tokens is the broker package's GitHub side: a container's installation tokens, which the launcher never sees.
// Errors are *protocol.Error, wrap docker.ErrUnreachable, or are opaque, carrying no Docker stderr.
type Tokens interface {
	Ready(app string) error              // manifest_rejected for another App; login_required or keychain_failed for its key
	BotUserID(app string) (int64, error) // login_required or keychain_failed for the bot user ID login github saved
	Issue(ctx context.Context, app string, permissions map[string]string, container string, exec func(argv []string, stdin []byte) error) ([]protocol.Expiry, error)
	Refresh(ctx context.Context, app string, permissions map[string]string, container string, exec func(argv []string, stdin []byte) error) ([]protocol.Expiry, error)
	Revoke(ctx context.Context, container string) error
	Pending() ([]protocol.Pending, error)                  // read-only backlog snapshot
	Retry(ctx context.Context) ([]protocol.Pending, error) // the backlog still unrevoked
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
	GitHub   Tokens
	Versions CodexVersions
	Releases Releases                          // nil: a build that must rebuild fails
	Progress io.Writer                         // nil: none; build's notes on older images, after Docker's progress; start's phase times
	Lock     func() (unlock func(), err error) // the image lock: build's lease and inspect, and prune
	Audit    *audit.Recorder                   // nil: none; start, refresh and close name their container and add the client credential
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
		Docker:   d,
		Versions: codex.New(),
		Releases: releases.New(),
		Spawn:    func() error { return spawnCheck(home) },
	}
}

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
	log, err := os.OpenFile(filepath.Join(dir, "clients-check.log"), os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o600)
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
	Container           string            `json:"container"`
	Endpoint            string            `json:"endpoint"`
	ImageDigest         string            `json:"image_digest"`
	Clients             Clients           `json:"clients"`
	GitHub              []protocol.Expiry `json:"github"`
	GitHubUserID        *int64            `json:"github_user_id"`
	CredentialExpiresAt *string           `json:"credential_expires_at"`
}

type RefreshResult struct {
	Container           string            `json:"container"`
	GitHub              []protocol.Expiry `json:"github"`
	CredentialExpiresAt *string           `json:"credential_expires_at"`
}

type StatusResult struct {
	Revocations []protocol.Pending    `json:"revocations"`
	Codex       *protocol.CodexStatus `json:"codex,omitempty"`
	Claude      map[string]bool       `json:"claude,omitempty"`
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
