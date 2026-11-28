// Package docker is the only code that runs the Docker CLI.
package docker

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"sync"
	"time"
)

const (
	systemPath = "/usr/bin:/bin:/usr/sbin:/sbin"
	Path       = "/Applications/Docker.app/Contents/Resources/bin/docker"
	pluginDir  = "/Applications/Docker.app/Contents/Resources/cli-plugins"
	RunDir     = "/run/ensemble"
	SecretsDir = RunDir + "/secrets"
	capture    = 65536
)

// Credential request kinds' directories in a container (protocol credential-dirs.json).
const (
	GitHubDir = SecretsDir + "/github"
	LinearDir = SecretsDir + "/linear"
	EnvDir    = SecretsDir + "/env"
)

// InfoID: a remote CLI answers it from its probe, opening no second ssh connection.
var InfoID = []string{"info", "--format", "{{json .ID}}"}

var probeArgs = []string{"info", "--format", `{"id":{{json .ID}},"version":{{json .ServerVersion}}}`}

// probeTimeout: a remote that drops packets is unreachable then.
var probeTimeout = 15 * time.Second

type Runner interface {
	Run(ctx context.Context, args []string, stdin []byte) ([]byte, error)
}

var ErrUnreachable = errors.New("Docker target unreachable")

// Unreachable says the target and why, never a container's output.
type Unreachable struct {
	Endpoint, Reason string
}

func (e *Unreachable) Error() string {
	return "Cannot reach Docker target " + e.Endpoint + ": " + e.Reason + "."
}

func (e *Unreachable) Is(target error) bool { return target == ErrUnreachable }

const startDesktop = "; start Docker Desktop and retry"

// Newer CLIs pass the daemon's missing-network answer through.
var networkNotFound = regexp.MustCompile(`network \S+ not found`)

type CommandError struct {
	Exit   int
	Stderr string
}

func (e *CommandError) Error() string {
	return fmt.Sprintf("docker exited %d: %s", e.Exit, strings.TrimSpace(e.Stderr))
}

func Diagnostic(err error) string {
	var ce *CommandError
	if !errors.As(err, &ce) {
		return err.Error()
	}
	lines := strings.Split(ce.Stderr, "\n")
	for i := len(lines) - 1; i >= 0; i-- {
		line := strings.TrimSpace(lines[i])
		if line != "" && !strings.HasPrefix(line, "Run 'docker ") && !strings.HasPrefix(line, "See 'docker ") && !strings.HasPrefix(line, "View build details: ") {
			return line
		}
	}
	return err.Error()
}

func Absent(err error) bool {
	var ce *CommandError
	return errors.As(err, &ce) && (strings.Contains(ce.Stderr, "No such image") || strings.Contains(ce.Stderr, "No such object") ||
		strings.Contains(ce.Stderr, "No such container") || strings.Contains(ce.Stderr, "no such volume") ||
		strings.Contains(ce.Stderr, "No such network") || networkNotFound.MatchString(ce.Stderr))
}

func Conflict(err error) bool {
	var ce *CommandError
	return errors.As(err, &ce) && strings.Contains(ce.Stderr, "Conflict. The container name")
}

// TempDir avoids TMPDIR and /tmp: no container mounts its base, so no worker can rewrite it.
func TempDir(home, pattern string) (string, error) {
	base := filepath.Join(home, ".ensemble/docker/tmp")
	if err := os.MkdirAll(base, 0o700); err != nil {
		return "", err
	}
	return os.MkdirTemp(base, pattern)
}

type CLI struct {
	path, buildx, home, config string
	Progress                   io.Writer // gets each docker build's stderr live
	target                     Target
	host                       string
	targetErr                  error // an invalid target setting: every Run's error
	sshDir                     string
	mu                         sync.Mutex
	probed                     bool
	probeErr                   error
	probedID                   string
}

// New's CLI config ignores ~/.docker; Run checks executables, so local validation fails first.
func New(home string) (*CLI, func(), error) {
	dir, err := TempDir(home, "ensemble-docker-")
	if err != nil {
		return nil, nil, err
	}
	clean := func() { _ = os.RemoveAll(dir) }
	config, _ := json.Marshal(map[string]any{"cliPluginsExtraDirs": []string{pluginDir}})
	if err := os.WriteFile(filepath.Join(dir, "config.json"), config, 0o600); err != nil {
		clean()
		return nil, nil, err
	}
	target, targetErr := LoadTarget(home)
	return &CLI{path: Path, buildx: filepath.Join(pluginDir, "docker-buildx"), home: home, config: dir, target: target,
		targetErr: targetErr, sshDir: SSHDir}, clean, nil
}

func (c *CLI) Target() Target { return c.target }

func (c *CLI) TargetErr() error { return c.targetErr }

// Remotes are one CLI per target address, in order.
func (c *CLI) Remotes() []*CLI {
	remotes := make([]*CLI, 0, len(c.target.Hosts))
	for _, host := range c.target.Hosts {
		remotes = append(remotes, &CLI{path: c.path, buildx: c.buildx, home: c.home, config: c.config, Progress: c.Progress,
			target: c.target, targetErr: c.targetErr, host: host, sshDir: c.sshDir})
	}
	return remotes
}

func (c *CLI) Remote() bool { return c.host != "" }

func (c *CLI) Endpoint() string {
	if c.Remote() {
		return c.host
	}
	return "unix://" + filepath.Join(c.home, ".docker/run/docker.sock")
}

// Command passes a remote target only SSH_AUTH_SOCK beyond the fixed environment.
func (c *CLI) Command(args []string) (string, []string, []string) {
	argv := append([]string{"--config", c.config, "--host", c.Endpoint()}, args...)
	env := []string{"PATH=" + c.Path(), "HOME=" + c.home, "DOCKER_CONFIG=" + c.config, "DOCKER_BUILDKIT=1"}
	if sock := os.Getenv("SSH_AUTH_SOCK"); c.Remote() && sock != "" {
		env = append(env, "SSH_AUTH_SOCK="+sock)
	}
	return c.path, argv, env
}

func (c *CLI) Path() string {
	if c.Remote() {
		return c.sshDir + ":" + systemPath
	}
	return systemPath
}

// capped keeps the tail: a long build's error comes last.
type capped struct{ data []byte }

func (b *capped) Write(p []byte) (int, error) {
	b.data = append(b.data, p...)
	if over := len(b.data) - capture; over > 0 {
		b.data = b.data[over:]
	}
	return len(p), nil
}

func executable(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.Mode().IsRegular() && info.Mode()&0o111 != 0
}

// Run probes a remote target once; after a failed probe no docker process runs again.
func (c *CLI) Run(ctx context.Context, args []string, stdin []byte) ([]byte, error) {
	if c.targetErr != nil {
		return nil, c.targetErr
	}
	for _, path := range []string{c.path, c.buildx} {
		if !executable(path) {
			return nil, &Unreachable{Endpoint: c.Endpoint(), Reason: "Docker Desktop executable unavailable: " + path + startDesktop}
		}
	}
	if c.Remote() {
		if wrapper := filepath.Join(c.sshDir, "ssh"); !executable(wrapper) {
			return nil, &Unreachable{Endpoint: c.Endpoint(), Reason: "ssh wrapper unavailable: " + wrapper + "; reinstall the broker"}
		}
		if err := c.probe(ctx); err != nil {
			return nil, err
		}
		if c.probedID != "" && slices.Equal(args, InfoID) {
			if id, err := json.Marshal(c.probedID); err == nil {
				return append(id, '\n'), nil
			}
		}
	}
	out, stderr, err := c.exec(ctx, args, stdin)
	if err == nil {
		return out, nil
	}
	if u := c.unreachable(stderr); u != nil {
		return nil, u
	}
	var exit *exec.ExitError
	if errors.As(err, &exit) && ctx.Err() == nil {
		return nil, &CommandError{Exit: exit.ExitCode(), Stderr: stderr}
	}
	return nil, fmt.Errorf("docker: %w", errors.Join(err, ctx.Err()))
}

// probe's diagnostic comes from docker info, never a container; a probe its caller cut short is not kept.
func (c *CLI) probe(ctx context.Context) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.probed {
		return c.probeErr
	}
	bounded, cancel := context.WithTimeout(ctx, probeTimeout)
	defer cancel()
	out, stderr, err := c.exec(bounded, probeArgs, nil)
	if ctx.Err() != nil {
		return fmt.Errorf("docker: %w", ctx.Err())
	}
	c.probed = true
	if err != nil && ctx.Err() == nil && errors.Is(bounded.Err(), context.DeadlineExceeded) {
		c.probeErr = &Unreachable{Endpoint: c.Endpoint(), Reason: "connection failed (no answer within " + probeTimeout.String() + ")"}
		return c.probeErr
	}
	if err != nil {
		u := c.unreachable(stderr)
		if u == nil {
			u = &Unreachable{Endpoint: c.Endpoint(), Reason: "connection failed"}
		}
		u.Reason += " (" + connectDetail(stderr) + ")"
		c.probeErr = u
		return c.probeErr
	}
	var info struct {
		ID      string `json:"id"`
		Version string `json:"version"`
	}
	version := string(out)
	if json.Unmarshal(out, &info) == nil {
		version = info.Version
	}
	if reason := engine([]byte(version)); reason != "" {
		c.probeErr = &Unreachable{Endpoint: c.Endpoint(), Reason: reason}
		return c.probeErr
	}
	c.probedID = info.ID
	return nil
}

func (c *CLI) unreachable(stderr string) *Unreachable {
	if !c.Remote() {
		if strings.Contains(stderr, "Cannot connect to the Docker daemon") || strings.Contains(stderr, "docker.sock: connect:") {
			return &Unreachable{Endpoint: c.Endpoint(), Reason: "Docker daemon not running" + startDesktop}
		}
		return nil
	}
	if !strings.Contains(stderr, "error during connect") && !strings.Contains(stderr, "Cannot connect to the Docker daemon") {
		return nil
	}
	return &Unreachable{Endpoint: c.Endpoint(), Reason: remoteReason(stderr)}
}

func (c *CLI) exec(ctx context.Context, args []string, stdin []byte) ([]byte, string, error) {
	path, argv, env := c.Command(args)
	cmd := exec.CommandContext(ctx, path, argv...)
	cmd.Env = env
	cmd.Stdin = bytes.NewReader(stdin)
	cmd.WaitDelay = time.Second
	var stdout, stderr capped
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	if c.Progress != nil && len(args) > 0 && args[0] == "build" {
		cmd.Stderr = io.MultiWriter(&stderr, c.Progress)
		// The broker's own lines follow: start them on a fresh line.
		defer func() {
			if n := len(stderr.data); n > 0 && stderr.data[n-1] != '\n' {
				_, _ = c.Progress.Write([]byte{'\n'})
			}
		}()
	}
	err := cmd.Run()
	return stdout.data, string(stderr.data), err
}
