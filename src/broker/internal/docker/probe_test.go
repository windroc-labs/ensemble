package docker

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"
)

// probeFormat is the probe's argv after --host: $5 $6 $7 in a fake docker.
const probeFormat = `info --format {"id":{{json .ID}},"version":{{json .ServerVersion}}}`

func TestRemoteProbeAnswersTheDaemonID(t *testing.T) {
	t.Chdir(t.TempDir())
	c := remoteDocker(t, `echo "$5" >> calls; if [ "$5 $6 $7" = '`+probeFormat+`' ]; then echo '{"id":"AB:CD","version":"27.5.1"}'; else echo listed; fi`)
	for range 2 {
		out, err := c.Run(context.Background(), InfoID, nil)
		if err != nil || string(out) != "\"AB:CD\"\n" {
			t.Fatalf("%q %v", out, err)
		}
	}
	if out, err := c.Run(context.Background(), []string{"ps"}, nil); err != nil || string(out) != "listed\n" {
		t.Fatalf("%q %v", out, err)
	}
	calls, err := os.ReadFile("calls")
	if err != nil || string(calls) != "info\nps\n" {
		t.Fatalf("one probe answers the ID, then ps: %q", calls)
	}
}

func TestLocalRunsTheIDQuery(t *testing.T) {
	c := fakeDocker(t, `echo "$*"`)
	out, err := c.Run(context.Background(), InfoID, nil)
	if err != nil || !strings.HasSuffix(string(out), " info --format {{json .ID}}\n") {
		t.Fatalf("%q %v", out, err)
	}
}

func TestPath(t *testing.T) {
	local := &CLI{sshDir: SSHDir}
	remote := &CLI{sshDir: SSHDir, host: "ssh://u@pc"}
	if local.Path() != "/usr/bin:/bin:/usr/sbin:/sbin" || remote.Path() != SSHDir+":/usr/bin:/bin:/usr/sbin:/sbin" {
		t.Fatalf("%q %q", local.Path(), remote.Path())
	}
}

func TestLocalBesideARemoteTargetNeverProbes(t *testing.T) {
	t.Chdir(t.TempDir())
	if remotes := fakeDocker(t, "true").Remotes(); len(remotes) != 0 {
		t.Fatalf("a local target has remote CLIs: %+v", remotes)
	}
	local := fakeDocker(t, `echo "$*" >> calls; echo ok`)
	local.target = Target{Hosts: []string{"ssh://u@pc"}, LocalSecrets: []string{"ensemble-broker-env/mini-op-password"}}
	if local.Endpoint() != "unix://"+filepath.Join(local.home, ".docker/run/docker.sock") || local.Remote() {
		t.Fatalf("local CLI %+v", local)
	}
	if _, err := local.Run(context.Background(), []string{"ps"}, nil); err != nil {
		t.Fatal(err)
	}
	calls, err := os.ReadFile("calls")
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(calls), "info") || !strings.Contains(string(calls), "--host unix://") {
		t.Fatalf("local CLI calls %q", calls)
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
		"engine 25":      {`echo '{"id":"A","version":"25.0.5"}'`, "Docker Engine 25.0.5 is older than 26"},
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
		c := remoteDocker(t, `if [ "$5 $6 $7" = '`+probeFormat+`' ]; then echo '{"id":"A","version":"`+version+`"}'; else echo listed; fi`)
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
	calls, err := os.ReadFile("calls")
	if err != nil {
		t.Fatal(err)
	}
	if string(calls) != "info --format\n" {
		t.Fatalf("docker ran after the failed probe: %q", calls)
	}
}

func TestASilentTargetIsUnreachableAtTheProbeDeadline(t *testing.T) {
	t.Chdir(t.TempDir())
	saved := probeTimeout
	probeTimeout = 200 * time.Millisecond
	t.Cleanup(func() { probeTimeout = saved })
	// The sleep outlives the killed shell and holds its pipes, as an ssh left by a killed docker would: without a
	// bounded wait for them, Run hangs past the test timeout.
	c := remoteDocker(t, `echo "$5" >> calls; sleep 3600`)
	for range 2 {
		_, err := c.Run(context.Background(), []string{"ps"}, nil)
		var u *Unreachable
		if !errors.As(err, &u) || u.Reason != "connection failed (no answer within 200ms)" {
			t.Fatalf("%v", err)
		}
	}
	if calls, err := os.ReadFile("calls"); err != nil || string(calls) != "info\n" {
		t.Fatalf("one probe, then no docker: %q %v", calls, err)
	}
}

func TestACancelledProbeIsNotCached(t *testing.T) {
	t.Chdir(t.TempDir())
	// The first probe hangs until its caller gives up; the next one answers.
	c := remoteDocker(t, `echo "$5" >> calls; if [ ! -e first ]; then : > first; sleep 3600; fi; if [ "$5 $6 $7" = '`+probeFormat+`' ]; then echo '{"id":"AB:CD","version":"27.5.1"}'; else echo listed; fi`)
	ctx, cancel := context.WithCancel(context.Background())
	time.AfterFunc(200*time.Millisecond, cancel)
	_, err := c.Run(ctx, []string{"ps"}, nil)
	if errors.Is(err, ErrUnreachable) || !errors.Is(err, context.Canceled) {
		t.Fatalf("a probe its caller cancelled is no verdict on the target: %v", err)
	}
	if out, err := c.Run(context.Background(), []string{"ps"}, nil); err != nil || string(out) != "listed\n" {
		t.Fatalf("the next Run probes again: %q %v", out, err)
	}
	if calls, err := os.ReadFile("calls"); err != nil || string(calls) != "info\ninfo\nps\n" {
		t.Fatalf("two probes, then ps: %q %v", calls, err)
	}
}

// One remote CLI per address, in the setting's order, each with its own probe.
func TestRemotesFollowTheAddressList(t *testing.T) {
	t.Chdir(t.TempDir())
	local := fakeDocker(t, `echo "$4" >> calls; echo '{"id":"A","version":"27.5.1"}'`)
	local.sshDir = t.TempDir()
	if err := os.WriteFile(filepath.Join(local.sshDir, "ssh"), []byte("#!/bin/sh\nexit 255\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	local.target = Target{Hosts: []string{"ssh://u@lan", "ssh://u@ts"}, LocalSecrets: []string{"ensemble-broker-env/a"}}
	remotes := local.Remotes()
	if len(remotes) != 2 || remotes[0].Endpoint() != "ssh://u@lan" || remotes[1].Endpoint() != "ssh://u@ts" ||
		!remotes[0].Remote() || !slices.Equal(remotes[1].Target().LocalSecrets, []string{"ensemble-broker-env/a"}) {
		t.Fatalf("%+v", remotes)
	}
	for _, c := range remotes {
		if _, err := c.Run(context.Background(), InfoID, nil); err != nil {
			t.Fatal(err)
		}
	}
	if calls, err := os.ReadFile("calls"); err != nil || string(calls) != "ssh://u@lan\nssh://u@ts\n" {
		t.Fatalf("each address probed once: %q %v", calls, err)
	}
}
