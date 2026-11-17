package docker

import (
	"context"
	"os"
	"strings"
	"testing"
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
	remote := &CLI{sshDir: SSHDir, target: Target{Host: "ssh://u@pc"}}
	if local.Path() != "/usr/bin:/bin:/usr/sbin:/sbin" || remote.Path() != SSHDir+":/usr/bin:/bin:/usr/sbin:/sbin" {
		t.Fatalf("%q %q", local.Path(), remote.Path())
	}
}
