package launcher

import (
	"context"
	"errors"
	"slices"
	"strings"
	"testing"

	"ensemble/broker/internal/protocol"
)

const stateVol = "ensemble-state-worker_1"

func stateHelpers(dirs ...string) [][]string {
	helper := []string{"run", "--rm", "--log-driver", "none", "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges"}
	mount := "type=volume,source=" + stateVol + ",target=/state,volume-nocopy"
	chown := append(slices.Clone(helper), "--cap-add", "CHOWN", "--user", "0:0", "--mount", mount, "--entrypoint", "/bin/chown", digest, "1001:1002", "/state")
	mkdir := append(slices.Clone(helper), "--user", "1001:1002", "--mount", mount, "--entrypoint", "/bin/mkdir", digest, "-p", "-m", "0700")
	for _, dir := range dirs {
		mkdir = append(mkdir, "/state/"+dir)
	}
	return [][]string{chown, mkdir}
}

func called(d *fakeDocker, call []string) bool {
	return slices.ContainsFunc(d.calls, func(c []string) bool { return slices.Equal(c, call) })
}

func TestStateVolumeCreatedWithAgentDirs(t *testing.T) {
	for _, tc := range []struct {
		name string
		path func(*testing.T) string
		dirs []string
	}{
		{"claude", validManifest, []string{"claude"}},
		{"codex", func(t *testing.T) string { return codexManifest(t) }, []string{"codex", "codex/sessions", "codex/config"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			h, d := startHost(&world{labels: goodLabels()}, &fakeCreds{})
			if tc.name == "codex" {
				h.Codex = &fakeCodexCreds{}
			}
			if _, err := h.Start(context.Background(), tc.path(t)); err != nil {
				t.Fatal(err)
			}
			create := []string{"volume", "create", "--label", "ensemble.state=worker_1", "--label", "ensemble.state-owner=1001:1002", stateVol}
			if !called(d, create) {
				t.Fatalf("no state volume create: %q", d.calls)
			}
			for _, helper := range stateHelpers(tc.dirs...) {
				if !called(d, helper) {
					t.Fatalf("missing helper %q in %q", helper, d.calls)
				}
			}
			if slices.ContainsFunc(d.calls, func(c []string) bool { return slices.Contains(c, "--privileged") }) {
				t.Fatal("privileged helper")
			}
		})
	}
}

func TestExistingStateVolumeIsUsedAsIs(t *testing.T) {
	h, d := startHost(&world{labels: goodLabels(), state: "matching"}, &fakeCreds{})
	if _, err := h.Start(context.Background(), validManifest(t)); err != nil {
		t.Fatal(err)
	}
	for _, c := range d.calls {
		if strings.Join(c[:2], " ") == "volume create" && c[len(c)-1] == stateVol || slices.Contains(c, "/bin/mkdir") {
			t.Fatalf("touched an existing state volume: %q", c)
		}
	}
}

func TestForeignStateVolumeIsRejected(t *testing.T) {
	h, d := startHost(&world{labels: goodLabels(), state: "foreign"}, &fakeCreds{})
	_, err := h.Start(context.Background(), validManifest(t))
	e := protoErr(t, err)
	if e.Code != protocol.MountRejected || !strings.Contains(e.Message, stateVol) || detached(d) != nil || called(d, []string{"volume", "rm", stateVol}) {
		t.Fatalf("%+v %q", e, d.verbs())
	}
}

func TestStateHelperFailureRemovesTheVolume(t *testing.T) {
	h, d := startHost(&world{labels: goodLabels(), stateHelperErr: errors.New("boom")}, &fakeCreds{})
	_, err := h.Start(context.Background(), validManifest(t))
	e := protoErr(t, err)
	if e.Code != protocol.StartFailed || !called(d, []string{"volume", "rm", stateVol}) || detached(d) != nil {
		t.Fatalf("%+v %q", e, d.verbs())
	}
}

func TestFailedStartRemovesTheStateVolumeItCreated(t *testing.T) {
	h, d := startHost(&world{labels: goodLabels(), runErr: errors.New("boom")}, &fakeCreds{})
	if _, err := h.Start(context.Background(), validManifest(t)); err == nil || !called(d, []string{"volume", "rm", stateVol}) {
		t.Fatalf("%v %q", err, d.verbs())
	}
	h, d = startHost(&world{labels: goodLabels(), runErr: errors.New("boom"), state: "matching"}, &fakeCreds{})
	if _, err := h.Start(context.Background(), validManifest(t)); err == nil || called(d, []string{"volume", "rm", stateVol}) {
		t.Fatalf("removed a state volume it did not create: %v %q", err, d.verbs())
	}
}

func TestNoBindMountForAnyClient(t *testing.T) {
	for _, client := range []string{"claude", "codex", "opencode"} {
		m := baseManifest()
		m.Client, m.ClaudeDir = client, ""
		dir := home + "/.ensemble/docker-state/w/" + client
		switch client {
		case "claude":
			m.ClaudeDir = dir
		case "codex":
			m.CodexDir = dir
		case "opencode":
			m.OpencodeDir = dir
		}
		flags, list := mounts(m, goodLabels())
		for i, f := range flags {
			if f == "--mount" && strings.Contains(flags[i+1], "type=bind") {
				t.Fatalf("%s: %s", client, flags[i+1])
			}
		}
		for _, mount := range list {
			if mount.Type == "bind" {
				t.Fatalf("%s: audit %+v", client, mount)
			}
			if mount.Type == "volume" && mount.Target != m.CWD && mount.Source != "ensemble-state-w" {
				t.Fatalf("%s: audit %+v", client, mount)
			}
		}
	}
}

func TestAPrefetchingStartWhoseStateVolumeFailsRemovesItsNewWorkspace(t *testing.T) {
	w := &world{labels: goodLabels(), stateHelperErr: errors.New("boom")}
	h, d, _ := prefetchHost(t, w)
	if _, err := h.Start(context.Background(), prefetchManifest(t, h, nil)); err == nil {
		t.Fatal("started")
	}
	if !called(d, []string{"volume", "rm", volume}) || !called(d, []string{"volume", "rm", stateVol}) {
		t.Fatalf("volumes kept: %q", d.calls)
	}
}
