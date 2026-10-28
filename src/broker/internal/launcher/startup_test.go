package launcher

import (
	"context"
	"slices"
	"strings"
	"testing"

	"ensemble/broker/internal/docker"
)

func TestStartCombinedBootstrap(t *testing.T) {
	for _, hook := range []bool{false, true} {
		h, d := startHost(&world{labels: goodLabels()}, &fakeCreds{})
		path := validManifest(t)
		if !hook {
			path = fixtureManifest(t, "manifest-no-hook.json")
		}
		if _, err := h.Start(context.Background(), path); err != nil {
			t.Fatal(err)
		}
		var setup [][]string
		for i, args := range d.calls {
			if args[0] == "exec" && !slices.Contains(args, credentialBatch) {
				setup = append(setup, args)
				if !slices.Contains(args, "/usr/bin/python3") || !slices.Contains(args, "-I") || !strings.Contains(string(d.stdin[i]), "subprocess.run") {
					t.Fatalf("bootstrap must use isolated system Python over stdin: %q", args)
				}
				if slices.Contains(args, "root") != hook {
					t.Fatalf("unexpected bootstrap identity: %q", args)
				}
			}
		}
		if len(setup) != 1 {
			t.Fatalf("hook=%v: %d setup execs", hook, len(setup))
		}
		if slices.Contains(detached(d), "SETUID") != hook || slices.Contains(detached(d), "SETGID") != hook {
			t.Fatalf("identity capabilities must be hook-only: %q", detached(d))
		}
	}
}

func TestStartBootstrapFailureMessages(t *testing.T) {
	for exit, want := range map[int]string{41: "unreadable by agent", 42: "Writing client bootstrap state failed.", 43: "Installing the hook stub failed."} {
		h, _ := startHost(&world{labels: goodLabels(), execErr: failOn("/usr/bin/python3", &docker.CommandError{Exit: exit, Stderr: "opaque"})}, &fakeCreds{})
		_, err := h.Start(context.Background(), validManifest(t))
		if err == nil || !strings.Contains(protoErr(t, err).Message, want) {
			t.Fatalf("exit %d: %v", exit, err)
		}
	}
}
