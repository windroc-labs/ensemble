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

// writeTarget names the setting file itself, so the tests pin where users write it.
func writeTarget(t *testing.T, home, content string) {
	t.Helper()
	path := filepath.Join(home, ".ensemble/config.local.toml")
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
}

// dockerTable is a config.local.toml whose [docker] table holds body.
func dockerTable(body string) string { return "[docker]\n" + body + "\n" }

func TestLoadTarget(t *testing.T) {
	home := t.TempDir()
	if target, err := LoadTarget(home); err != nil || target.Remote() || target.Host != "" {
		t.Fatalf("no setting file: %+v %v", target, err)
	}
	for _, host := range []string{"ssh://pc", "ssh://u@pc", "ssh://first.last@pc.tail1234.ts.net:2222", "ssh://u@192.168.1.20:22", "ssh://pc:65535"} {
		writeTarget(t, home, dockerTable(`host = "`+host+`"`+"\nlocal_secrets = []"))
		if target, err := LoadTarget(home); err != nil || !target.Remote() || target.Host != host {
			t.Fatalf("%s: %+v %v", host, target, err)
		}
	}
	file := filepath.Join(home, ".ensemble/config.local.toml")
	table := file + " [docker]"
	form := "invalid target setting (line 1: write the target as a plain [docker] table)"
	for _, c := range []struct{ content, endpoint, why string }{
		{"[docker", file, "invalid target setting (line 1: "},
		{`docker = "ssh://pc"`, file, form},
		{"[[docker]]\nhost = \"ssh://pc\"", file, form},
		{"[docker.extra]", file, form},
		{"docker.host = \"ssh://pc\"\ndocker.local_secrets = []", file, form},
		{`docker = { host = "ssh://pc", local_secrets = [] }`, file, form},
		{dockerTable("") + dockerTable(""), file, "invalid target setting (line 3: [docker] repeated)"},
		{dockerTable(`host = "ssh://pc"` + "\nhost = \"ssh://u@pc\""), file, "invalid target setting (line 3: host repeated)"},
		{dockerTable(`a.b = 1`), file, `invalid target setting (line 2: unknown key "a.b")`},
		{dockerTable(`host = "ssh://pc" x`), file, "invalid target setting (line 2: unexpected text after a value)"},
		{dockerTable(`host = "ssh://pc`), file, "invalid target setting (line 2: unclosed string)"},
		{`"\u0064ocker" = 1`, file, "invalid target setting (line 1: a quoted key must be a plain single-line string)"},
		{dockerTable(""), table, "invalid target setting (no host)"},
		{dockerTable("host = 1"), table, "invalid target setting (host is not a plain single-line string)"},
		{dockerTable(`host = "ssh:\u002f/pc"`), table, "invalid target setting (host is not a plain single-line string)"},
		{dockerTable(`host = """ssh://pc"""`), table, "invalid target setting (host is not a plain single-line string)"},
		{dockerTable(`host = "ssh://pc"` + "\nuser = \"u\"\nlocal_secrets = []"), table, `invalid target setting (unknown key "user")`},
		{dockerTable(`HOST = "ssh://pc"` + "\nhost = \"ssh://pc\"\nlocal_secrets = []"), table, `invalid target setting (unknown key "HOST")`},
		{dockerTable(`host = "ssh://pc\n"`), table, `invalid target setting (host is not a plain`},
		{dockerTable(`host = "ssh://-oProxyCommand=x"`), table, "invalid target setting (host"},
		{dockerTable(`host = "tcp://pc:2375"`), table, "invalid target setting (host"},
		{dockerTable(`host = "ssh://pc/var/run/x"`), table, "invalid target setting (host"},
		{dockerTable(`host = "ssh://pc?x=1"`), table, "invalid target setting (host"},
		{dockerTable(`host = "ssh://pc:0"`), table, "invalid target setting (host"},
		{dockerTable(`host = "ssh://pc:65536"`), table, "invalid target setting (host"},
		{dockerTable(`host = "ssh://u:pw@pc"`), table, "invalid target setting (host"},
		{dockerTable(`host = ""`), table, "invalid target setting (host"},
	} {
		writeTarget(t, home, c.content)
		_, err := LoadTarget(home)
		var u *Unreachable
		if !errors.As(err, &u) || u.Endpoint != c.endpoint || !strings.HasPrefix(u.Reason, c.why) {
			t.Errorf("%q: %v", c.content, err)
		}
	}
	writeTarget(t, home, dockerTable(`host = "ssh://pc"`+"\nlocal_secrets = []")+"# "+strings.Repeat("x", 1<<20)+"\n")
	var u *Unreachable
	if _, err := LoadTarget(home); !errors.As(err, &u) || u.Endpoint != file || !strings.Contains(u.Reason, "larger than") {
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
	for line := range strings.SplitSeq(string(out), "\n") {
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
	calls, err := os.ReadFile("calls")
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(calls), "info") {
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
	writeTarget(t, home, dockerTable(`host = "ssh://u@pc"`+"\nlocal_secrets = []"))
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
	writeTarget(t, home, dockerTable(`host = "tcp://pc"`))
	c, clean, err := New(home)
	if err != nil {
		t.Fatal(err)
	}
	defer clean()
	_, err = c.Run(context.Background(), []string{"ps"}, nil)
	var u *Unreachable
	if !errors.As(err, &u) || u.Endpoint != filepath.Join(home, ".ensemble/config.local.toml")+" [docker]" || !strings.HasPrefix(u.Reason, "invalid target setting (") {
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

func TestRemoteCommandLosingTheTarget(t *testing.T) {
	c := remoteDocker(t, `if [ "$5" = info ]; then echo '{"id":"A","version":"27.5.1"}'; exit 0; fi; echo 'error during connect: stderr=container says Permission denied' >&2; exit 1`)
	_, err := c.Run(context.Background(), []string{"exec", "c", "sh"}, nil)
	var u *Unreachable
	if !errors.As(err, &u) || u.Reason != "authentication failed" {
		t.Fatalf("%v", err)
	}
	ok := remoteDocker(t, `if [ "$5" = info ]; then echo '{"id":"A","version":"27.5.1"}'; exit 0; fi; echo 'cat: x: No such file' >&2; exit 2`)
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
