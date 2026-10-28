package docker

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"ensemble/broker/internal/fixtures"
)

func TestBoundary(t *testing.T) {
	t.Setenv("HOME", "/hostile")
	t.Setenv("DOCKER_HOST", "tcp://hostile:1234")
	t.Setenv("DOCKER_CONTEXT", "hostile")
	t.Setenv("PATH", "/hostile")
	c := &CLI{path: Path, home: "/Users/test", config: "/private/config"}
	path, args, env := c.Command([]string{"inspect", "image"})
	want := []string{"--config", "/private/config", "--host", "unix:///Users/test/.docker/run/docker.sock", "inspect", "image"}
	if path != "/Applications/Docker.app/Contents/Resources/bin/docker" || !reflect.DeepEqual(args, want) {
		t.Fatalf("unexpected command %s %q", path, args)
	}
	wantEnv := []string{"PATH=/usr/bin:/bin:/usr/sbin:/sbin", "HOME=/Users/test", "DOCKER_CONFIG=/private/config", "DOCKER_BUILDKIT=1"}
	if !reflect.DeepEqual(env, wantEnv) {
		t.Fatalf("env %q", env)
	}
	if c.Endpoint() != "unix:///Users/test/.docker/run/docker.sock" {
		t.Fatal(c.Endpoint())
	}
}

func fakeDocker(t *testing.T, script string) *CLI {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "docker")
	if err := os.WriteFile(path, []byte("#!/bin/sh\n"+script), 0o755); err != nil {
		t.Fatal(err)
	}
	return &CLI{path: path, buildx: path, home: dir, config: dir}
}

func TestRunResults(t *testing.T) {
	out, err := fakeDocker(t, `cat; echo " $*"`).Run(context.Background(), []string{"ps"}, []byte("in"))
	if err != nil || !strings.HasPrefix(string(out), "in --config ") || !strings.HasSuffix(string(out), " ps\n") {
		t.Fatalf("%q %v", out, err)
	}
	_, err = fakeDocker(t, "echo nope >&2; exit 3").Run(context.Background(), nil, nil)
	var ce *CommandError
	if !errors.As(err, &ce) || ce.Exit != 3 || ce.Stderr != "nope\n" || errors.Is(err, ErrUnreachable) {
		t.Fatalf("%#v", err)
	}
}

func TestUnreachable(t *testing.T) {
	for _, msg := range []string{
		"Cannot connect to the Docker daemon at unix:///Users/x/.docker/run/docker.sock. Is the docker daemon running?",
		"error during connect: dial unix /Users/x/.docker/run/docker.sock: connect: no such file or directory",
	} {
		_, err := fakeDocker(t, "echo '"+msg+"' >&2; exit 1").Run(context.Background(), nil, nil)
		if !errors.Is(err, ErrUnreachable) {
			t.Fatalf("%q: %v", msg, err)
		}
	}
}

func TestMissingExecutableIsUnreachable(t *testing.T) {
	present := fakeDocker(t, "touch ran").path
	for _, c := range []*CLI{{path: "/nonexistent/docker", buildx: present}, {path: present, buildx: "/nonexistent/docker-buildx"}} {
		_, err := c.Run(context.Background(), []string{"ps"}, nil)
		if !errors.Is(err, ErrUnreachable) || !strings.Contains(err.Error(), "executable unavailable: /nonexistent/") {
			t.Fatalf("%q: %v", c.path, err)
		}
	}
}

func TestConfigOutsideTMPDIR(t *testing.T) {
	home := t.TempDir()
	t.Setenv("TMPDIR", t.TempDir())
	c, clean, err := New(home)
	if err != nil {
		t.Fatal(err)
	}
	base := filepath.Join(home, ".ensemble/docker/tmp")
	for _, dir := range []string{base, c.config} {
		if info, err := os.Lstat(dir); err != nil || !info.IsDir() || info.Mode().Perm() != 0o700 {
			t.Fatalf("%s: %v %v", dir, info, err)
		}
	}
	if filepath.Dir(c.config) != base {
		t.Fatalf("config %s outside %s", c.config, base)
	}
	if data, err := os.ReadFile(filepath.Join(c.config, "config.json")); err != nil || string(data) != `{"cliPluginsExtraDirs":["`+pluginDir+`"]}` {
		t.Fatalf("%s %v", data, err)
	}
	clean()
	if _, err := os.Stat(c.config); !os.IsNotExist(err) {
		t.Fatalf("config kept: %v", err)
	}
}

func TestDiagnostic(t *testing.T) {
	failed := `ERROR: failed to solve: process "/bin/sh -c apt-get purge -y sudo" did not complete successfully: exit code: 1`
	for stderr, want := range map[string]string{
		"#7 0.412 E: Sub-process /usr/bin/dpkg returned an error code (1)\n#7 ERROR: process \"/bin/sh -c apt-get purge -y sudo\" did not complete successfully: exit code: 1\n" +
			failed + "\n\nView build details: docker-desktop://dashboard/build/desktop-linux/desktop-linux/abc123\n": failed,
		"docker: Error response from daemon: mounts denied\n\nRun 'docker run --help' for more information\n": "docker: Error response from daemon: mounts denied",
	} {
		if got := Diagnostic(&CommandError{Exit: 1, Stderr: stderr}); got != want {
			t.Errorf("%q: got %q", stderr, got)
		}
	}
	if got := Diagnostic(errors.New("boom")); got != "boom" {
		t.Fatal(got)
	}
}

func TestRunKeepsOutputTail(t *testing.T) {
	_, err := fakeDocker(t, "head -c 70000 /dev/zero | tr '\\0' x >&2; printf '\\nERROR: last\\n' >&2; exit 1").Run(context.Background(), nil, nil)
	var ce *CommandError
	if !errors.As(err, &ce) || len(ce.Stderr) != capture || Diagnostic(err) != "ERROR: last" {
		t.Fatalf("%v", err)
	}
}

func TestRunCopiesStderrToProgress(t *testing.T) {
	c := fakeDocker(t, "echo '#1 load' >&2; echo out; echo '#2 ERROR: boom' >&2; exit 1")
	var progress bytes.Buffer
	c.Progress = &progress
	out, err := c.Run(context.Background(), []string{"build"}, nil)
	var ce *CommandError
	if out != nil || !errors.As(err, &ce) || ce.Stderr != "#1 load\n#2 ERROR: boom\n" || progress.String() != ce.Stderr {
		t.Fatalf("%q %v %q", out, err, progress.String())
	}
	progress.Reset()
	if _, err := c.Run(context.Background(), []string{"image", "inspect"}, nil); err == nil || progress.Len() != 0 {
		t.Fatalf("%v %q", err, progress.String())
	}
}

func TestRunEndsBuildProgressOnALine(t *testing.T) {
	for script, want := range map[string]string{
		"printf '#7 partial' >&2; exit 1": "#7 partial\n",
		"printf '#7 done\\n' >&2":         "#7 done\n",
		"exit 0":                          "",
	} {
		var progress bytes.Buffer
		c := fakeDocker(t, script)
		c.Progress = &progress
		_, _ = c.Run(context.Background(), []string{"build"}, nil)
		if progress.String() != want {
			t.Errorf("%s: %q, want %q", script, progress.String(), want)
		}
	}
}

func TestAbsentNetwork(t *testing.T) {
	for stderr, want := range map[string]bool{
		"Error: No such network: egress-workers":                       true,
		"Error response from daemon: network egress-workers not found": true,
		"Error response from daemon: permission denied":                false,
		"Error response from daemon: network egress-workers is in use": false,
	} {
		if got := Absent(&CommandError{Exit: 1, Stderr: stderr}); got != want {
			t.Errorf("Absent(%q) = %v", stderr, got)
		}
	}
}

func TestCredentialDirsFixture(t *testing.T) {
	data, err := os.ReadFile(filepath.Join(fixtures.Dir(), "credential-dirs.json"))
	if err != nil {
		t.Fatal(err)
	}
	var dirs map[string]string
	if err := json.Unmarshal(data, &dirs); err != nil {
		t.Fatal(err)
	}
	want := map[string]string{"github": GitHubDir, "linear": LinearDir, "env": EnvDir}
	if !reflect.DeepEqual(dirs, want) {
		t.Fatalf("credential-dirs.json %v, want %v", dirs, want)
	}
	for kind, dir := range want {
		if dir != SecretsDir+"/"+kind {
			t.Errorf("%s: %s", kind, dir)
		}
	}
}
