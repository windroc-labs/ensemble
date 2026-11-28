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
	if target, err := LoadTarget(home); err != nil || target.Remote() || target.Hosts != nil {
		t.Fatalf("no setting file: %+v %v", target, err)
	}
	for _, host := range []string{"ssh://pc", "ssh://u@pc", "ssh://first.last@pc.tail1234.ts.net:2222", "ssh://u@192.168.1.20:22", "ssh://pc:65535"} {
		writeTarget(t, home, dockerTable(`host = "`+host+`"`+"\nlocal_secrets = []"))
		if target, err := LoadTarget(home); err != nil || !target.Remote() || !slices.Equal(target.Hosts, []string{host}) {
			t.Fatalf("%s: %+v %v", host, target, err)
		}
	}
	endpoint := filepath.Join(home, ".ensemble/config.local.toml") + " [docker]"
	for content, why := range map[string]string{
		"[docker":                         "invalid target setting (",
		`docker = "ssh://pc"`:             "invalid target setting ([docker] is not a table)",
		"[[docker]]\nhost = \"ssh://pc\"": "invalid target setting ([docker] is not a table)",
		dockerTable(""):                   "invalid target setting (no host)",
		dockerTable("host = 1"):           "invalid target setting (host is not a string)",
		dockerTable(`host = "ssh://pc"` + "\nuser = \"u\"\nlocal_secrets = []"):                `invalid target setting (unknown key "user")`,
		dockerTable(`HOST = "ssh://pc"` + "\nhost = \"ssh://pc\"\nlocal_secrets = []"):         `invalid target setting (unknown key "HOST")`,
		dockerTable(`host = "ssh://pc"` + "\nhost = \"ssh://u@pc\""):                           "invalid target setting (",
		dockerTable(`host = "ssh://pc\n"`):                                                     `invalid target setting (host "ssh://pc\n"`,
		dockerTable(`host = "ssh://-oProxyCommand=x"`):                                         "invalid target setting (host",
		dockerTable(`host = "tcp://pc:2375"`):                                                  "invalid target setting (host",
		dockerTable(`host = "ssh://pc/var/run/x"`):                                             "invalid target setting (host",
		dockerTable(`host = "ssh://pc?x=1"`):                                                   "invalid target setting (host",
		dockerTable(`host = "ssh://pc:0"`):                                                     "invalid target setting (host",
		dockerTable(`host = "ssh://pc:65536"`):                                                 "invalid target setting (host",
		dockerTable(`host = "ssh://u:pw@pc"`):                                                  "invalid target setting (host",
		dockerTable(`host = ""`):                                                               "invalid target setting (host",
		dockerTable(`host = "ssh://pc"` + "\nhosts = [\"ssh://ts\"]\nlocal_secrets = []"):      "invalid target setting (host and hosts both set)",
		dockerTable(`hosts = "ssh://pc"` + "\nlocal_secrets = []"):                             "invalid target setting (hosts is not an array)",
		dockerTable(`hosts = []` + "\nlocal_secrets = []"):                                     "invalid target setting (hosts is empty)",
		dockerTable(`hosts = [1]` + "\nlocal_secrets = []"):                                    `invalid target setting (hosts item "1" is not ssh://[user@]host[:port])`,
		dockerTable(`hosts = ["ssh://pc:0"]` + "\nlocal_secrets = []"):                         `invalid target setting (hosts item "ssh://pc:0" is not ssh://[user@]host[:port])`,
		dockerTable(`hosts = ["ssh://pc", "ssh://-oProxyCommand=x"]` + "\nlocal_secrets = []"): `invalid target setting (hosts item "ssh://-oProxyCommand=x" is not`,
		dockerTable(`hosts = ["ssh://pc", "ssh://pc"]` + "\nlocal_secrets = []"):               `invalid target setting (hosts item "ssh://pc" repeated)`,
		dockerTable(`hosts = ["ssh://a", "ssh://b", "ssh://c", "ssh://d", "ssh://e", "ssh://f", "ssh://g", "ssh://h", "ssh://i"]` + "\nlocal_secrets = []"): "invalid target setting (more than 8 hosts)",
		dockerTable(`hosts = ["ssh://pc"]`): "invalid target setting (no local_secrets)",
	} {
		writeTarget(t, home, content)
		_, err := LoadTarget(home)
		var u *Unreachable
		if !errors.As(err, &u) || u.Endpoint != endpoint || !strings.HasPrefix(u.Reason, why) {
			t.Fatalf("%q: %v", content, err)
		}
	}
	writeTarget(t, home, dockerTable(`host = "ssh://pc"`+"\nlocal_secrets = []")+"# "+strings.Repeat("x", 1<<20)+"\n")
	if _, err := LoadTarget(home); err == nil || !strings.Contains(err.Error(), "larger than") {
		t.Fatalf("oversized file: %v", err)
	}
	unreadable := t.TempDir()
	if err := os.MkdirAll(filepath.Join(unreadable, SettingFile), 0o700); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadTarget(unreadable); !errors.As(err, new(*Unreachable)) || !strings.Contains(err.Error(), "invalid target setting (") {
		t.Fatalf("unreadable file: %v", err)
	}
}

// TestTargetSettings: the broker reads only [docker], whatever else the shared config file holds.
func TestTargetSettings(t *testing.T) {
	sixteen := `["ensemble-broker-env/a", "ensemble-broker-env/b", "ensemble-broker-env/c", "ensemble-broker-env/d", "ensemble-broker-env/e", "ensemble-broker-env/f", "ensemble-broker-env/g", "ensemble-broker-env/h", "ensemble-broker-env/i", "ensemble-broker-env/j", "ensemble-broker-env/k", "ensemble-broker-env/l", "ensemble-broker-env/m", "ensemble-broker-env/n", "ensemble-broker-env/o", "ensemble-broker-env/p"`
	others := "[session.hosts]\nhidden_size = \"120x40\"\n\n" +
		"[delegate.roles.engineer]\nnote = \"\"\"\n[docker]\nhost = \"ssh://evil\"\n\"\"\"\n\n" +
		"[[delegate.environments.mini-op.credentials.env]]\nname = \"x\"\nitem = { kind = \"env\" }\n"
	for _, c := range []struct {
		content      string
		hosts        []string
		localSecrets []string
	}{
		{others, nil, nil},
		{"", nil, nil},
		{dockerTable(`host = "ssh://u@pc"` + "\nlocal_secrets = [\"ensemble-broker-env/mini-op-password\", \"ensemble-broker-env/mini-op-ssh-key\"]"), []string{"ssh://u@pc"}, []string{"ensemble-broker-env/mini-op-password", "ensemble-broker-env/mini-op-ssh-key"}},
		{dockerTable("local_secrets = []\nhost = 'ssh://pc:2222'"), []string{"ssh://pc:2222"}, []string{}},
		{dockerTable(`hosts = ["ssh://u@192.168.1.20", "ssh://u@pc.tail1234.ts.net:2222"]` + "\nlocal_secrets = []"), []string{"ssh://u@192.168.1.20", "ssh://u@pc.tail1234.ts.net:2222"}, []string{}},
		{dockerTable(`hosts = ["ssh://a", "ssh://b", "ssh://c", "ssh://d", "ssh://e", "ssh://f", "ssh://g", "ssh://h"]` + "\nlocal_secrets = []"), []string{"ssh://a", "ssh://b", "ssh://c", "ssh://d", "ssh://e", "ssh://f", "ssh://g", "ssh://h"}, []string{}},
		{dockerTable(`host = "ssh://pc"` + "\nlocal_secrets = " + sixteen + "]"), []string{"ssh://pc"}, []string{"ensemble-broker-env/a", "ensemble-broker-env/b", "ensemble-broker-env/c", "ensemble-broker-env/d", "ensemble-broker-env/e", "ensemble-broker-env/f", "ensemble-broker-env/g", "ensemble-broker-env/h", "ensemble-broker-env/i", "ensemble-broker-env/j", "ensemble-broker-env/k", "ensemble-broker-env/l", "ensemble-broker-env/m", "ensemble-broker-env/n", "ensemble-broker-env/o", "ensemble-broker-env/p"}},
		{others + dockerTable(`host = "ssh://u@pc"`+"\nlocal_secrets = [\n  \"ensemble-broker-env/a\", # kept on the Mac\n]"), []string{"ssh://u@pc"}, []string{"ensemble-broker-env/a"}},
		{"docker.host = \"ssh://pc\"\ndocker.local_secrets = []\n", []string{"ssh://pc"}, []string{}},
	} {
		home := t.TempDir()
		writeTarget(t, home, c.content)
		target, err := LoadTarget(home)
		if err != nil || !slices.Equal(target.Hosts, c.hosts) || !slices.Equal(target.LocalSecrets, c.localSecrets) {
			t.Errorf("%s: %+v %v", c.content, target, err)
		}
	}
	for _, body := range []string{
		`host = "ssh://pc"`,
		"local_secrets = []",
		`host = "ssh://pc"` + "\nlocal_secrets = \"ensemble-broker-env/mini-op-password\"",
		`host = "ssh://pc"` + "\nlocal_secrets = [1]",
		`host = "ssh://pc"` + "\nlocal_secrets = [\"mini-op-password\"]",
		`host = "ssh://pc"` + "\nlocal_secrets = [\"ensemble-broker-claude/x\"]",
		`host = "ssh://pc"` + "\nlocal_secrets = [\"ensemble-broker-env/Mini\"]",
		`host = "ssh://pc"` + "\nlocal_secrets = [\"ensemble-broker-env/a\", \"ensemble-broker-env/a\"]",
		`host = "ssh://pc"` + "\nlocal_secrets = " + sixteen + `, "ensemble-broker-env/q"]`,
		`host = "ssh://pc"` + "\nlocal_secrets = []\nextra = 1",
		`host = "ssh://pc"` + "\nlocal_secrets = [\"ensemble-broker-env/a\"]\nLOCAL_SECRETS = []",
		`host = "ssh://pc"` + "\nlocal_secrets = [[\"ensemble-broker-env/a\"]]",
		`host = "ssh://pc"` + "\nlocal_secrets = []\n[docker.extra]",
	} {
		home := t.TempDir()
		writeTarget(t, home, dockerTable(body))
		_, err := LoadTarget(home)
		var u *Unreachable
		if !errors.As(err, &u) || !strings.HasPrefix(u.Reason, "invalid target setting (") {
			t.Errorf("%s: %v", body, err)
		}
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
	c.target = Target{Hosts: []string{"ssh://u@pc"}}
	c.host = "ssh://u@pc"
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
	local, clean, err := New(home)
	if err != nil {
		t.Fatal(err)
	}
	defer clean()
	c := local.Remotes()[0]
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
	c := &CLI{path: Path, home: "/Users/test", config: "/private/config", host: "ssh://u@pc", sshDir: SSHDir}
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
