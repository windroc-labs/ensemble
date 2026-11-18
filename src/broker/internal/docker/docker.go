// Package docker is the only code that runs the Docker CLI: fixed binary, fixed endpoint, clean environment.
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
	"strings"
)

const (
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

type Runner interface {
	Run(ctx context.Context, args []string, stdin []byte) ([]byte, error)
}

var ErrUnreachable = errors.New("Docker Desktop daemon unreachable")

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

// TempDir makes a private directory under <home>/.ensemble/docker/tmp, never TMPDIR or /tmp:
// ~/.ensemble is on the launcher's mount deny list, so no worker can mount it and rewrite the contents.
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
}

// New writes a private CLI config that ignores ~/.docker; Run checks Docker Desktop's executables,
// so a command's local validation fails first.
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
	return &CLI{path: Path, buildx: filepath.Join(pluginDir, "docker-buildx"), home: home, config: dir}, clean, nil
}

func (c *CLI) Endpoint() string {
	return "unix://" + filepath.Join(c.home, ".docker/run/docker.sock")
}

func (c *CLI) Command(args []string) (string, []string, []string) {
	argv := append([]string{"--config", c.config, "--host", c.Endpoint()}, args...)
	env := []string{"PATH=/usr/bin:/bin:/usr/sbin:/sbin", "HOME=" + c.home, "DOCKER_CONFIG=" + c.config, "DOCKER_BUILDKIT=1"}
	return c.path, argv, env
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

func (c *CLI) Run(ctx context.Context, args []string, stdin []byte) ([]byte, error) {
	for _, path := range []string{c.path, c.buildx} {
		info, err := os.Stat(path)
		if err != nil || !info.Mode().IsRegular() || info.Mode()&0o111 == 0 {
			return nil, fmt.Errorf("%w: Docker Desktop executable unavailable: %s", ErrUnreachable, path)
		}
	}
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
	if err == nil {
		return stdout.data, nil
	}
	text := string(stderr.data)
	if strings.Contains(text, "Cannot connect to the Docker daemon") || strings.Contains(text, "docker.sock: connect:") {
		return nil, fmt.Errorf("%w: %s", ErrUnreachable, strings.TrimSpace(text))
	}
	var exit *exec.ExitError
	if errors.As(err, &exit) && ctx.Err() == nil {
		return nil, &CommandError{Exit: exit.ExitCode(), Stderr: text}
	}
	return nil, fmt.Errorf("docker: %w", errors.Join(err, ctx.Err()))
}
