// Package docker is the only code that runs the Docker CLI: fixed binary, the user's one Docker target, clean environment.
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
)

const (
	systemPath = "/usr/bin:/bin:/usr/sbin:/sbin"
	Path       = "/Applications/Docker.app/Contents/Resources/bin/docker"
	pluginDir  = "/Applications/Docker.app/Contents/Resources/cli-plugins"
	RunDir     = "/run/ensemble"
	SecretsDir = RunDir + "/secrets"
	capture    = 65536
)

// A credential request kind's directory in a container (protocol credential-dirs.json).
const (
	GitHubDir = SecretsDir + "/github"
	LinearDir = SecretsDir + "/linear"
	EnvDir    = SecretsDir + "/env"
)

// InfoID asks for the daemon's ID; a remote CLI answers it from its probe, so identifying a remote target opens no
// second ssh connection.
var InfoID = []string{"info", "--format", "{{json .ID}}"}

var probeArgs = []string{"info", "--format", `{"id":{{json .ID}},"version":{{json .ServerVersion}}}`}

type Runner interface {
	Run(ctx context.Context, args []string, stdin []byte) ([]byte, error)
}

var ErrUnreachable = errors.New("Docker target unreachable")

// Unreachable is the only error for a Docker target the CLI cannot reach: the target and why, never a container's
// output. It matches ErrUnreachable.
type Unreachable struct {
	Endpoint, Reason string
}

func (e *Unreachable) Error() string {
	return "Cannot reach Docker target " + e.Endpoint + ": " + e.Reason + "."
}

func (e *Unreachable) Is(target error) bool { return target == ErrUnreachable }

const startDesktop = "; start Docker Desktop and retry"

// networkNotFound is the daemon's answer for a missing network, as newer CLIs pass it through.
var networkNotFound = regexp.MustCompile(`network \S+ not found`)

type CommandError struct {
	Exit   int
	Stderr string
}

func (e *CommandError) Error() string {
	return fmt.Sprintf("docker exited %d: %s", e.Exit, strings.TrimSpace(e.Stderr))
}

// Diagnostic is Docker's diagnostic: the last stderr line that is not the CLI's --help trailer
// or Docker Desktop's build-details link.
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

// Absent reports whether Docker says the image, object, container, volume or network does not exist.
func Absent(err error) bool {
	var ce *CommandError
	return errors.As(err, &ce) && (strings.Contains(ce.Stderr, "No such image") || strings.Contains(ce.Stderr, "No such object") ||
		strings.Contains(ce.Stderr, "No such container") || strings.Contains(ce.Stderr, "no such volume") ||
		strings.Contains(ce.Stderr, "No such network") || networkNotFound.MatchString(ce.Stderr))
}

// Conflict reports whether Docker refused a container name another container holds.
func Conflict(err error) bool {
	var ce *CommandError
	return errors.As(err, &ce) && strings.Contains(ce.Stderr, "Conflict. The container name")
}

// TempDir makes a private directory under <home>/.ensemble/docker/tmp, never TMPDIR or /tmp, which no container
// mounts, so no worker can rewrite the contents.
func TempDir(home, pattern string) (string, error) {
	base := filepath.Join(home, ".ensemble/docker/tmp")
	if err := os.MkdirAll(base, 0o700); err != nil {
		return "", err
	}
	return os.MkdirTemp(base, pattern)
}

type CLI struct {
	path, buildx, home, config string
	Progress                   io.Writer // nil: none; gets each docker build's stderr as it arrives
	target                     Target
	targetErr                  error  // an invalid TargetFile: every Run's error
	sshDir                     string // SSHDir; tests' own
	mu                         sync.Mutex
	probed                     bool
	probeErr                   error
	probedID                   string
}

// New writes a private CLI config that ignores ~/.docker and reads the Docker target; Run checks Docker Desktop's
// executables, so a command's local validation fails first.
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

// Target is the Docker target setting c was made with.
func (c *CLI) Target() Target { return c.target }

// TargetErr is the invalid target setting's error; nil for a valid one or none.
func (c *CLI) TargetErr() error { return c.targetErr }

// Local is a CLI for the local Docker Desktop beside c's remote target, with c's config and Progress; nil when c's
// target is the local Docker Desktop.
func (c *CLI) Local() *CLI {
	if !c.target.Remote() {
		return nil
	}
	return &CLI{path: c.path, buildx: c.buildx, home: c.home, config: c.config, Progress: c.Progress, sshDir: c.sshDir}
}

func (c *CLI) Endpoint() string {
	if c.target.Remote() {
		return c.target.Host
	}
	return "unix://" + filepath.Join(c.home, ".docker/run/docker.sock")
}

// Command is docker's argv and environment; a remote target's also put the ssh wrapper first on PATH and pass the
// ssh agent's socket, the one variable added for ssh.
func (c *CLI) Command(args []string) (string, []string, []string) {
	argv := append([]string{"--config", c.config, "--host", c.Endpoint()}, args...)
	env := []string{"PATH=" + c.Path(), "HOME=" + c.home, "DOCKER_CONFIG=" + c.config, "DOCKER_BUILDKIT=1"}
	if sock := os.Getenv("SSH_AUTH_SOCK"); c.target.Remote() && sock != "" {
		env = append(env, "SSH_AUTH_SOCK="+sock)
	}
	return c.path, argv, env
}

// Path is the PATH docker runs with: a remote target's puts the ssh wrapper first.
func (c *CLI) Path() string {
	if c.target.Remote() {
		return c.sshDir + ":" + systemPath
	}
	return systemPath
}

// capped keeps the last capture bytes: a long build's error comes last.
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

// Run runs docker args against the target. A remote target is first probed once: reached over ssh, with docker on
// the remote and an Engine of at least minEngine; once the probe fails, no docker process runs again.
func (c *CLI) Run(ctx context.Context, args []string, stdin []byte) ([]byte, error) {
	if c.targetErr != nil {
		return nil, c.targetErr
	}
	for _, path := range []string{c.path, c.buildx} {
		if !executable(path) {
			return nil, &Unreachable{Endpoint: c.Endpoint(), Reason: "Docker Desktop executable unavailable: " + path + startDesktop}
		}
	}
	if c.target.Remote() {
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
	if u := c.unreachable(stderr, false); u != nil {
		return nil, u
	}
	var exit *exec.ExitError
	if errors.As(err, &exit) && ctx.Err() == nil {
		return nil, &CommandError{Exit: exit.ExitCode(), Stderr: stderr}
	}
	return nil, fmt.Errorf("docker: %w", errors.Join(err, ctx.Err()))
}

// probe checks a remote target once per CLI, keeping its daemon's ID; its diagnostic comes from docker info, never a
// container.
func (c *CLI) probe(ctx context.Context) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.probed {
		return c.probeErr
	}
	c.probed = true
	out, stderr, err := c.exec(ctx, probeArgs, nil)
	if err != nil {
		u := c.unreachable(stderr, true)
		if u == nil {
			u = &Unreachable{Endpoint: c.Endpoint(), Reason: "connection failed (" + connectDetail(stderr) + ")"}
		}
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

// unreachable classifies a failed docker's stderr by target: nil when the daemon answered.
func (c *CLI) unreachable(stderr string, detailed bool) *Unreachable {
	if !c.target.Remote() {
		if strings.Contains(stderr, "Cannot connect to the Docker daemon") || strings.Contains(stderr, "docker.sock: connect:") {
			return &Unreachable{Endpoint: c.Endpoint(), Reason: "Docker daemon not running" + startDesktop}
		}
		return nil
	}
	if !strings.Contains(stderr, "error during connect") && !strings.Contains(stderr, "Cannot connect to the Docker daemon") {
		return nil
	}
	reason := remoteReason(stderr)
	if detailed {
		reason += " (" + connectDetail(stderr) + ")"
	}
	return &Unreachable{Endpoint: c.Endpoint(), Reason: reason}
}

// exec runs docker args: its stdout, its stderr's tail, and the run's error.
func (c *CLI) exec(ctx context.Context, args []string, stdin []byte) ([]byte, string, error) {
	path, argv, env := c.Command(args)
	cmd := exec.CommandContext(ctx, path, argv...)
	cmd.Env = env
	cmd.Stdin = bytes.NewReader(stdin)
	var stdout, stderr capped
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	if c.Progress != nil && len(args) > 0 && args[0] == "build" {
		cmd.Stderr = io.MultiWriter(&stderr, c.Progress)
		// The broker's own lines (error object, image notes) follow: start them on a fresh line.
		defer func() {
			if n := len(stderr.data); n > 0 && stderr.data[n-1] != '\n' {
				_, _ = c.Progress.Write([]byte{'\n'})
			}
		}()
	}
	err := cmd.Run()
	return stdout.data, string(stderr.data), err
}
