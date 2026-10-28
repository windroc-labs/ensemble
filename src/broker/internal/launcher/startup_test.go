package launcher

import (
	"context"
	"slices"
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
		for _, args := range d.calls {
			if args[0] == "exec" && !slices.Contains(args, credentialBatch) {
				setup = append(setup, args)
				if !slices.Contains(args, "-I") {
					t.Fatalf("bootstrap must use isolated system Python over stdin: %q", args)
				}
				if slices.Contains(args, "root") != slices.Contains(args, "hook") {
					t.Fatalf("unexpected bootstrap identity: %q", args)
				}
			}
		}
		want := 1
		if hook {
			want++
		}
		if len(setup) != want {
			t.Fatalf("hook=%v: %d setup execs", hook, len(setup))
		}
	}
}

func TestStartBootstrapFailureMessages(t *testing.T) {
	for exit, want := range map[int]string{41: "Image content under /opt/ensemble is unreadable by agent; rebuild with ensemble-broker build.", 42: "Writing client bootstrap state failed.", 64: "Container bootstrap failed.", 1: "Container bootstrap failed."} {
		h, _ := startHost(&world{labels: goodLabels(), execErr: failOn("/usr/bin/python3", &docker.CommandError{Exit: exit, Stderr: "opaque"})}, &fakeCreds{})
		_, err := h.Start(context.Background(), validManifest(t))
		if err == nil || protoErr(t, err).Message != want {
			t.Fatalf("exit %d: %v", exit, err)
		}
	}
}
