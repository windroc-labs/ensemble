package docker

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"testing"
)

func writeTarget(t *testing.T, home, content string) {
	t.Helper()
	path := filepath.Join(home, TargetFile)
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
}

func TestLoadTarget(t *testing.T) {
	home := t.TempDir()
	if target, err := LoadTarget(home); err != nil || target.Remote() || target.Host != "" {
		t.Fatalf("no target file: %+v %v", target, err)
	}
	for _, host := range []string{"ssh://pc", "ssh://u@pc", "ssh://first.last@pc.tail1234.ts.net:2222", "ssh://u@192.168.1.20:22", "ssh://pc:65535"} {
		writeTarget(t, home, `{"host": "`+host+`"}`)
		if target, err := LoadTarget(home); err != nil || !target.Remote() || target.Host != host {
			t.Fatalf("%s: %+v %v", host, target, err)
		}
	}
	path := filepath.Join(home, TargetFile)
	for content, why := range map[string]string{
		``:                                   "invalid target setting (",
		`[]`:                                 "invalid target setting (",
		`null`:                               "invalid target setting (no host)",
		`{}`:                                 "invalid target setting (no host)",
		`{"host": 1}`:                        "invalid target setting (",
		`{"host": "ssh://pc", "user": "u"}`:  "invalid target setting (",
		`{"host": "ssh://pc"} {}`:            "invalid target setting (trailing data)",
		`{"host": "ssh://pc\n"}`:             `invalid target setting (host "ssh://pc\n"`,
		`{"host": "ssh://-oProxyCommand=x"}`: "invalid target setting (host",
		`{"host": "tcp://pc:2375"}`:          "invalid target setting (host",
		`{"host": "ssh://pc/var/run/x"}`:     "invalid target setting (host",
		`{"host": "ssh://pc?x=1"}`:           "invalid target setting (host",
		`{"host": "ssh://pc:0"}`:             "invalid target setting (host",
		`{"host": "ssh://pc:65536"}`:         "invalid target setting (host",
		`{"host": "ssh://u:pw@pc"}`:          "invalid target setting (host",
		`{"host": ""}`:                       "invalid target setting (host",
	} {
		writeTarget(t, home, content)
		_, err := LoadTarget(home)
		var u *Unreachable
		if !errors.As(err, &u) || u.Endpoint != path || !strings.HasPrefix(u.Reason, why) {
			t.Fatalf("%q: %v", content, err)
		}
	}
	writeTarget(t, home, `{"host": "ssh://pc", "pad": "`+strings.Repeat("x", 70000)+`"}`)
	if _, err := LoadTarget(home); err == nil || !strings.Contains(err.Error(), "larger than") {
		t.Fatalf("oversized file: %v", err)
	}
}

func TestSSHArgsOverrideLaterOptions(t *testing.T) {
	args := SSHArgs([]string{"-o", "ConnectTimeout=30", "-o", "ForwardAgent=yes", "-o", "ForwardX11=yes", "-T", "-l", "u", "--", "pc", "docker", "system", "dial-stdio"})
	if !slices.Equal(args[:2], []string{"-F", "/dev/null"}) || !slices.Equal(args[len(args)-4:], []string{"pc", "docker", "system", "dial-stdio"}) {
		t.Fatalf("%q", args)
	}
	// ssh -G prints the configuration a connection would use, without connecting.
	out, err := exec.Command(SSHPath, append([]string{"-G"}, args[:len(args)-3]...)...).Output()
	if err != nil {
		t.Fatalf("%s -G: %v", SSHPath, err)
	}
	got := map[string]string{}
	for _, line := range strings.Split(string(out), "\n") {
		if key, value, ok := strings.Cut(line, " "); ok {
			got[strings.ToLower(key)] = value
		}
	}
	for key, want := range map[string]string{
		"forwardagent": "no", "forwardx11": "no", "forwardx11trusted": "no", "clearallforwardings": "yes",
		"batchmode": "yes", "stricthostkeychecking": "true", "updatehostkeys": "false",
		"passwordauthentication": "no", "kbdinteractiveauthentication": "no", "gssapiauthentication": "no",
		"hostbasedauthentication": "no", "pubkeyauthentication": "true", "preferredauthentications": "publickey",
		"controlmaster": "false", "user": "u", "hostname": "pc",
	} {
		if got[key] != want {
			t.Errorf("%s: got %q, want %q", key, got[key], want)
		}
	}
}

// remoteDocker is a CLI for target ssh://u@pc whose docker is script and whose ssh wrapper exists.
func remoteDocker(t *testing.T, script string) *CLI {
	t.Helper()
	c := fakeDocker(t, script)
	c.target = Target{Host: "ssh://u@pc"}
	c.sshDir = t.TempDir()
	if err := os.WriteFile(filepath.Join(c.sshDir, "ssh"), []byte("#!/bin/sh\nexit 255\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	return c
}

func TestLocalTargetNeverProbes(t *testing.T) {
	t.Chdir(t.TempDir())
	c := fakeDocker(t, `echo "$*" >> calls; echo ok`)
	if _, err := c.Run(context.Background(), []string{"ps"}, nil); err != nil {
		t.Fatal(err)
	}
	calls, _ := os.ReadFile("calls")
	if strings.Contains(string(calls), "version") {
		t.Fatalf("probed a local target: %q", calls)
	}
}

func TestRemoteTargetIgnoresHostileEnvironment(t *testing.T) {
	home := t.TempDir()
	hostile := t.TempDir()
	if err := os.WriteFile(filepath.Join(hostile, "config.json"), []byte(`{"currentContext": "hostile"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("DOCKER_HOST", "tcp://hostile:1234")
	t.Setenv("DOCKER_CONTEXT", "hostile")
	t.Setenv("DOCKER_CONFIG", hostile)
	t.Setenv("SSH_AUTH_SOCK", "")
	writeTarget(t, home, `{"host": "ssh://u@pc"}`)
	c, clean, err := New(home)
	if err != nil {
		t.Fatal(err)
	}
	defer clean()
	_, args, env := c.Command([]string{"ps"})
	if !slices.Equal(args, []string{"--config", c.config, "--host", "ssh://u@pc", "ps"}) || c.Endpoint() != "ssh://u@pc" {
		t.Fatalf("%q %s", args, c.Endpoint())
	}
	want := []string{"PATH=" + SSHDir + ":/usr/bin:/bin:/usr/sbin:/sbin", "HOME=" + home, "DOCKER_CONFIG=" + c.config, "DOCKER_BUILDKIT=1"}
	if !reflect.DeepEqual(env, want) {
		t.Fatalf("env %q", env)
	}
}

func TestRemoteEnvironmentAddsOnlySSHAuthSock(t *testing.T) {
	c := &CLI{path: Path, home: "/Users/test", config: "/private/config", target: Target{Host: "ssh://u@pc"}, sshDir: SSHDir}
	t.Setenv("SSH_AUTH_SOCK", "/private/agent.sock")
	t.Setenv("SSH_AGENT_PID", "1")
	_, _, env := c.Command([]string{"ps"})
	want := []string{"PATH=" + SSHDir + ":/usr/bin:/bin:/usr/sbin:/sbin", "HOME=/Users/test", "DOCKER_CONFIG=/private/config", "DOCKER_BUILDKIT=1", "SSH_AUTH_SOCK=/private/agent.sock"}
	if !reflect.DeepEqual(env, want) {
		t.Fatalf("env %q", env)
	}
}

func TestInvalidTargetFailsEveryCommand(t *testing.T) {
	home := t.TempDir()
	writeTarget(t, home, `{"host": "tcp://pc"}`)
	c, clean, err := New(home)
	if err != nil {
		t.Fatal(err)
	}
	defer clean()
	_, err = c.Run(context.Background(), []string{"ps"}, nil)
	var u *Unreachable
	if !errors.As(err, &u) || u.Endpoint != filepath.Join(home, TargetFile) || !strings.HasPrefix(u.Reason, "invalid target setting (") {
		t.Fatalf("%v", err)
	}
}

func TestRemoteTargetRequiresTheWrapper(t *testing.T) {
	t.Chdir(t.TempDir())
	c := remoteDocker(t, `touch ran`)
	if err := os.Remove(filepath.Join(c.sshDir, "ssh")); err != nil {
		t.Fatal(err)
	}
	_, err := c.Run(context.Background(), []string{"ps"}, nil)
	var u *Unreachable
	if !errors.As(err, &u) || u.Endpoint != "ssh://u@pc" || u.Reason != "ssh wrapper unavailable: "+filepath.Join(c.sshDir, "ssh")+"; reinstall the broker" {
		t.Fatalf("%v", err)
	}
	if _, err := os.Stat("ran"); err == nil {
		t.Fatal("docker ran without the ssh wrapper")
	}
}

func TestProbeReasons(t *testing.T) {
	connect := `error during connect: Get "http://docker.example.com/v1.47/version": command [ssh -o ConnectTimeout=30 -T -l u -- pc docker system dial-stdio] has exited with exit status 255, make sure the URL is valid, and Docker 18.09 or later is installed on the remote host: stderr=`
	for name, tc := range map[string]struct{ script, reason string }{
		"unreachable": {`echo '` + connect + `ssh: connect to host pc port 22: Connection refused' >&2; exit 1`,
			"connection failed (ssh: connect to host pc port 22: Connection refused)"},
		"unresolved": {`echo '` + connect + `ssh: Could not resolve hostname pc: Name or service not known' >&2; exit 1`,
			"connection failed (ssh: Could not resolve hostname pc: Name or service not known)"},
		"key refused": {`echo '` + connect + `u@pc: Permission denied (publickey).' >&2; exit 1`,
			"authentication failed (u@pc: Permission denied (publickey).)"},
		"host key unknown": {`printf '%s\n' '` + connect + `No ED25519 host key is known for pc and you have requested strict checking.' 'Host key verification failed.' >&2; exit 1`,
			"host key unknown: not in ~/.ssh/known_hosts (Host key verification failed.)"},
		"host key changed": {`printf '%s\n' '` + connect + `@@@@@@@@@@@' '@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @' 'Host key verification failed.' >&2; exit 1`,
			"host key mismatch: differs from ~/.ssh/known_hosts (Host key verification failed.)"},
		"no docker (sh)": {`echo '` + connect + `bash: line 1: docker: command not found' >&2; exit 1`,
			"no docker CLI on the remote login shell's PATH (bash: line 1: docker: command not found)"},
		"no docker (cmd)": {`echo '` + connect + `'"'"'docker'"'"' is not recognized as an internal or external command,' >&2; exit 1`,
			"no docker CLI on the remote login shell's PATH ('docker' is not recognized as an internal or external command,)"},
		"daemon down": {`echo '` + connect + `Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?' >&2; exit 1`,
			"Docker daemon not running (Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?)"},
		"other failure":  {`echo 'Error response from daemon: boom' >&2; exit 1`, "connection failed (Error response from daemon: boom)"},
		"engine 25":      {`echo 25.0.5`, "Docker Engine 25.0.5 is older than 26"},
		"no version":     {`echo dev`, "unrecognized Docker Engine version dev"},
		"strange output": {`printf '\033[31mred\n'`, "unrecognized Docker Engine version"},
	} {
		c := remoteDocker(t, tc.script)
		_, err := c.Run(context.Background(), []string{"ps"}, nil)
		var u *Unreachable
		if !errors.As(err, &u) || u.Endpoint != "ssh://u@pc" || u.Reason != tc.reason {
			t.Errorf("%s: %v", name, err)
		}
		if err != nil && !strings.HasPrefix(err.Error(), "Cannot reach Docker target ssh://u@pc: ") {
			t.Errorf("%s: %q", name, err)
		}
	}
	for _, version := range []string{"26.0.0-rc1", "26.1.4", "27.5.1"} {
		c := remoteDocker(t, `if [ "$5 $6 $7" = "version --format {{.Server.Version}}" ]; then echo `+version+`; else echo listed; fi`)
		if out, err := c.Run(context.Background(), []string{"ps"}, nil); err != nil || string(out) != "listed\n" {
			t.Errorf("%s: %q %v", version, out, err)
		}
	}
}

func TestFailedProbeRunsNoFurtherDocker(t *testing.T) {
	t.Chdir(t.TempDir())
	c := remoteDocker(t, `echo "$5 $6" >> calls; printf '%s\n' 'error during connect: stderr=@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @' 'Host key verification failed.' >&2; exit 1`)
	for _, args := range [][]string{{"build", "."}, {"run", "image"}, {"exec", "-i", "c", "sh"}} {
		if _, err := c.Run(context.Background(), args, []byte("secret")); !errors.Is(err, ErrUnreachable) {
			t.Fatalf("%q: %v", args, err)
		}
	}
	calls, _ := os.ReadFile("calls")
	if string(calls) != "version --format\n" {
		t.Fatalf("docker ran after the failed probe: %q", calls)
	}
}

func TestRemoteCommandLosingTheTarget(t *testing.T) {
	c := remoteDocker(t, `if [ "$5" = version ]; then echo 27.5.1; exit 0; fi; echo 'error during connect: stderr=container says Permission denied' >&2; exit 1`)
	_, err := c.Run(context.Background(), []string{"exec", "c", "sh"}, nil)
	var u *Unreachable
	if !errors.As(err, &u) || u.Reason != "authentication failed" {
		t.Fatalf("%v", err)
	}
	ok := remoteDocker(t, `if [ "$5" = version ]; then echo 27.5.1; exit 0; fi; echo 'cat: x: No such file' >&2; exit 2`)
	_, err = ok.Run(context.Background(), []string{"exec", "c", "cat", "x"}, nil)
	var ce *CommandError
	if !errors.As(err, &ce) || errors.Is(err, ErrUnreachable) {
		t.Fatalf("%v", err)
	}
}

func TestTargetPatternMatchesSchema(t *testing.T) {
	data, err := os.ReadFile("../../protocol/docker-broker/protocol.schema.json")
	if err != nil {
		t.Fatal(err)
	}
	var schema struct {
		Defs struct {
			Endpoint struct {
				Pattern string `json:"pattern"`
			} `json:"endpoint"`
		} `json:"$defs"`
	}
	if err := json.Unmarshal(data, &schema); err != nil {
		t.Fatal(err)
	}
	want := `^(unix:///[^\u0000-\u001f\u007f]+/\.docker/run/docker\.sock|` + TargetPattern + `)$`
	if schema.Defs.Endpoint.Pattern != want {
		t.Fatalf("schema endpoint pattern %q, want %q", schema.Defs.Endpoint.Pattern, want)
	}
}

func TestRemoteEndpoint(t *testing.T) {
	for endpoint, want := range map[string]bool{
		"ssh://u@pc":    true,
		"ssh://pc:2222": true,
		"unix:///Users/u/.docker/run/docker.sock": false,
		"":       false,
		"ssh://": false,
	} {
		if got := RemoteEndpoint(endpoint); got != want {
			t.Errorf("RemoteEndpoint(%q) = %v, want %v", endpoint, got, want)
		}
	}
}
