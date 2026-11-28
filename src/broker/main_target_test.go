package main

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"testing"

	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/launcher"
	"ensemble/broker/internal/protocol"
)

func TestTargetCommandRefusesAnInvalidSetting(t *testing.T) {
	home := t.TempDir()
	path := filepath.Join(home, docker.SettingFile)
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("[docker]\nhost = \"ssh://u@pc\"\nlocal_secrets = []\nextra = 1\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	a := system()
	a.keychain = func(bool) (keys, error) { return keys{Home: home}, nil }
	code, out, errOut := invoke(a, "target")
	assertError(t, errOut, out, "target", protocol.DockerUnreachable)
	if code != 7 || !strings.Contains(errOut, path) || !strings.Contains(errOut, "invalid target setting") {
		t.Fatalf("%d %s", code, errOut)
	}
}

// A home without Docker Desktop: every daemon is unreachable, so commands that name one fail before any change.
func TestDaemonCommandsWithoutDockerDesktop(t *testing.T) {
	home := t.TempDir()
	a := system()
	a.keychain = func(bool) (keys, error) { return keys{Home: home}, nil }
	c := "ensemble-w-0123456789abcdef0123456789abcdef"
	for _, args := range [][]string{
		{"close", "--container", c, "--daemon", "D1"},
		{"workspace-rm", "--volume", "ensemble-ws-w", "--force", "--daemon", "D1"},
	} {
		code, out, errOut := invoke(a, args...)
		assertError(t, errOut, out, args[0], protocol.DockerUnreachable)
		if code != 7 || !strings.Contains(errOut, "Docker Desktop executable unavailable") {
			t.Fatalf("%q: %d %s", args, code, errOut)
		}
	}
	code, out, errOut := invoke(a, "target")
	if code != 0 || !strings.Contains(out, `"default":"local"`) || !strings.Contains(out, `"unreachable":"Docker Desktop executable unavailable`) {
		t.Fatalf("%d %s %s", code, out, errOut)
	}
}

func TestTargetCommandWithAnUnwritableHome(t *testing.T) {
	home := filepath.Join(t.TempDir(), "file")
	if err := os.WriteFile(home, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	a := system()
	a.keychain = func(bool) (keys, error) { return keys{Home: home}, nil }
	code, out, errOut := invoke(a, "target")
	assertError(t, errOut, out, "target", protocol.InternalError)
	if code != protocol.Exit(protocol.InternalError) {
		t.Fatalf("%d %s", code, errOut)
	}
}

func TestSSHModeExecsSystemSSH(t *testing.T) {
	var path string
	var argv, env []string
	t.Setenv("SSH_AUTH_SOCK", "/private/agent.sock")
	failed := errors.New("exec failed")
	err := execSSH([]string{"-o", "ForwardAgent=yes", "-T", "--", "pc", "docker", "system", "dial-stdio"},
		func(p string, a, e []string) error { path, argv, env = p, a, e; return failed })
	want := append([]string{"ssh"}, docker.SSHArgs([]string{"-o", "ForwardAgent=yes", "-T", "--", "pc", "docker", "system", "dial-stdio"})...)
	if path != "/usr/bin/ssh" || !reflect.DeepEqual(argv, want) || argv[1] != "-F" || !slices.Contains(env, "SSH_AUTH_SOCK=/private/agent.sock") {
		t.Fatalf("%s %q", path, argv)
	}
	if err != failed {
		t.Fatalf("%v", err)
	}
}

func TestCloseAndWorkspaceRmNameTheirDaemon(t *testing.T) {
	c := "ensemble-w-0123456789abcdef0123456789abcdef"
	for _, args := range [][]string{
		{"close", "--container", c, "--daemon", "AB:CD-1"},
		{"workspace-rm", "--volume", "v", "--daemon", "AB:CD-1"},
		{"workspace-rm", "--volume", "v", "--force", "--daemon", "AB:CD-1"},
	} {
		got := ""
		a := app{close: func(_ context.Context, _ keys, container, daemon string) (any, error) {
			got = daemon
			return map[string]any{"container": container, "removed": true}, nil
		}, workspaceRm: func(_ context.Context, _ keys, volume string, _ bool, daemon string) (any, error) {
			got = daemon
			return map[string]any{"volume": volume, "removed": true}, nil
		}}
		if code, _, errOut := invoke(a, args...); code != 0 || got != "AB:CD-1" {
			t.Fatalf("%q: exit %d daemon %q: %s", args, code, got, errOut)
		}
	}
	for _, args := range [][]string{
		{"close", "--container", c},
		{"close", "--container", c, "--daemon"},
		{"close", "--container", c, "--daemon", "a b"},
		{"workspace-rm", "--volume", "v"},
		{"workspace-rm", "--volume", "v", "--force"},
		{"workspace-rm", "--volume", "v", "--daemon", "D", "--force"},
		{"workspace-rm", "--volume", "v", "--daemon", ""},
	} {
		if code, _, _ := invoke(app{}, args...); code != protocol.Exit(protocol.Usage) {
			t.Fatalf("%q: exit %d, want usage", args, code)
		}
	}
}

func TestWithHostDaemons(t *testing.T) {
	home := t.TempDir()
	d, clean, err := docker.New(home)
	if err != nil {
		t.Fatal(err)
	}
	defer clean()
	local := launcher.Daemon{Name: launcher.DaemonLocal, Endpoint: "unix://" + home + "/.docker/run/docker.sock", Path: "/usr/bin:/bin:/usr/sbin:/sbin"}
	if got := daemons(d); len(got) != 1 || got[0].Runner != d || withoutRunner(got[0]) != local {
		t.Fatalf("no target: %+v", got)
	}
	setting := filepath.Join(home, docker.SettingFile)
	if err := os.WriteFile(setting, []byte("[docker]\nhost = \"ssh://u@pc\"\nlocal_secrets = [\"ensemble-broker-env/mini-op-password\"]\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	d, clean, err = docker.New(home)
	if err != nil {
		t.Fatal(err)
	}
	defer clean()
	got := daemons(d)
	remote := launcher.Daemon{Name: launcher.DaemonRemote, Endpoint: "ssh://u@pc", Remote: true, Path: docker.SSHDir + ":/usr/bin:/bin:/usr/sbin:/sbin"}
	if len(got) != 2 || got[0].Runner == nil || withoutRunner(got[0]) != remote || got[1].Runner != d || withoutRunner(got[1]) != local {
		t.Fatalf("remote target: %+v", got)
	}
	if err := os.WriteFile(setting, []byte("[docker]\nhosts = [\"ssh://u@lan\", \"ssh://u@ts\"]\nlocal_secrets = []\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	d, clean, err = docker.New(home)
	if err != nil {
		t.Fatal(err)
	}
	defer clean()
	got = daemons(d)
	lan, ts := remote, remote
	lan.Endpoint, ts.Endpoint = "ssh://u@lan", "ssh://u@ts"
	if len(got) != 3 || withoutRunner(got[0]) != lan || withoutRunner(got[1]) != ts || got[0].Runner == got[1].Runner ||
		got[2].Runner != d || withoutRunner(got[2]) != local {
		t.Fatalf("address list: %+v", got)
	}
}

func withoutRunner(d launcher.Daemon) launcher.Daemon {
	d.Runner = nil
	return d
}
