package launcher

import (
	"context"
	"errors"
	"regexp"
	"slices"
	"strings"
	"testing"
	"time"

	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/protocol"
)

const workspace = home + "/src/project"

var image = "ensemble-worker:" + imageKey

// check is a scripted Docker for workspace-rm: each field steers one kind of call.
type check struct {
	labels     string // volume inspect output; "" absent
	images     string // image ls output
	out        string // check container stdout
	runErr     error
	rmErr      error
	inspect    error
	readers    string // the state readers' ps output
	stateRmErr error  // the state volume's rm
}

func (c *check) docker() *fakeDocker {
	return &fakeDocker{respond: func(args []string, stdin []byte) ([]byte, error) {
		switch strings.Join(args[:2], " ") {
		case "volume inspect":
			if c.inspect != nil {
				return nil, c.inspect
			}
			if c.labels == "" {
				return nil, &docker.CommandError{Exit: 1, Stderr: "Error response from daemon: get " + volume + ": no such volume"}
			}
			return []byte(c.labels + "\n"), nil
		case "image ls":
			return []byte(c.images), nil
		case "image inspect":
			return inspectJSON(digest, goodLabels(), time.Time{}), nil
		case "volume rm":
			if args[2] == stateVol {
				return []byte(stateVol + "\n"), c.stateRmErr
			}
			return []byte(volume + "\n"), c.rmErr
		case "ps --all":
			return []byte(c.readers), nil
		case "rm --force":
			return []byte(strings.Join(args[2:], "\n") + "\n"), nil
		}
		if args[0] == "run" {
			return []byte(c.out), c.runErr
		}
		return nil, errors.New("unexpected " + strings.Join(args, " "))
	}}
}

func labelled(path string) string {
	return `{"ensemble.kind":"ws","ensemble.workspace":"` + path + `"}`
}

func clean() *check {
	return &check{labels: labelled(workspace), images: "ensemble-worker:<none>\nensemble-worker:" + imageKey + "\nensemble-worker:" + strings.Repeat("c", 64) + "\n",
		out: `{"dirty": []}` + "\n"}
}

func workspaceRm(c *check, force bool) (WorkspaceResult, *fakeDocker, error) {
	d := c.docker()
	rm := Host{}.WorkspaceRm
	if force {
		rm = Host{}.WorkspaceRmForce
	}
	got, err := rm(context.Background(), Daemon{Runner: d}, volume)
	return got, d, err
}

func TestWorkspaceRmClean(t *testing.T) {
	got, d, err := workspaceRm(clean(), false)
	if err != nil || got != (WorkspaceResult{Volume: volume, Removed: true}) {
		t.Fatalf("%+v %v", got, err)
	}
	if want := []string{"volume inspect", "image ls", "image inspect", "run --rm", "ps --all", "volume rm", "volume rm"}; !slices.Equal(d.verbs(), want) {
		t.Fatalf("%q", d.calls)
	}
	readers := []string{"ps", "--all", "--quiet", "--filter", "volume=" + stateVol, "--filter", "label=ensemble.state-reader"}
	if !slices.Equal(d.calls[4], readers) || !slices.Equal(d.calls[5], []string{"volume", "rm", stateVol}) || !slices.Equal(d.calls[6], []string{"volume", "rm", volume}) {
		t.Fatalf("state before workspace: %q", d.calls[4:])
	}
	if want := []string{"image", "ls", "--filter", "reference=ensemble-worker", "--format", "{{.Repository}}:{{.Tag}}"}; !slices.Equal(d.calls[1], want) {
		t.Fatalf("%q", d.calls[1])
	}
	if !slices.Equal(d.calls[2], []string{"image", "inspect", image}) {
		t.Fatalf("newest image not used: %q", d.calls[2])
	}
	run := d.calls[3]
	if !regexp.MustCompile(`^ensemble-check-[0-9a-f]{32}$`).MatchString(run[3]) {
		t.Fatalf("check name %q", run[3])
	}
	want := []string{"run", "--rm", "--name", run[3], "--label", "ensemble.check=1", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
		"--user", "agent", "--network", "none", "--mount", "type=volume,source=" + volume + ",target=" + workspace + ",readonly,volume-nocopy",
		"--entrypoint", "/opt/ensemble/venv/bin/python", digest, "-I", "/opt/ensemble/lib/workspace-check", workspace, ".ensemble-cache"}
	if !slices.Equal(run, want) {
		t.Fatalf("check run\n got %q\nwant %q", run, want)
	}
}

func TestWorkspaceRmAbsentAndForce(t *testing.T) {
	for _, force := range []bool{false, true} {
		got, d, err := workspaceRm(&check{}, force)
		if err != nil || got != (WorkspaceResult{Volume: volume}) || !slices.Equal(d.verbs(), []string{"volume inspect", "ps --all", "volume rm"}) ||
			d.calls[2][2] != stateVol {
			t.Fatalf("absent force=%v: %+v %v %q", force, got, err, d.calls)
		}
	}
	c := clean()
	c.out = `{"dirty": ["src/o/a"]}`
	got, d, err := workspaceRm(c, true)
	if err != nil || !got.Removed || !slices.Equal(d.verbs(), []string{"volume inspect", "ps --all", "volume rm", "volume rm"}) {
		t.Fatalf("%+v %v %q", got, err, d.calls)
	}
}

func TestWorkspaceRmStopsReadersThenRemovesState(t *testing.T) {
	c := clean()
	c.readers = "r1\nr2\n"
	_, d, err := workspaceRm(c, true)
	if err != nil || !slices.Equal(d.verbs(), []string{"volume inspect", "ps --all", "rm --force", "volume rm", "volume rm"}) ||
		!slices.Equal(d.calls[2], []string{"rm", "--force", "r1", "r2"}) {
		t.Fatalf("%v %q", err, d.calls)
	}
}

func TestWorkspaceRmAbsentStateIsSuccess(t *testing.T) {
	c := clean()
	c.stateRmErr = &docker.CommandError{Exit: 1, Stderr: "Error response from daemon: get " + stateVol + ": no such volume"}
	got, _, err := workspaceRm(c, true)
	if err != nil || !got.Removed {
		t.Fatalf("%+v %v", got, err)
	}
}

func TestWorkspaceRmKeepsTheWorkspaceWhenStateRemovalFails(t *testing.T) {
	c := clean()
	c.stateRmErr = &docker.CommandError{Exit: 1, Stderr: "volume is in use"}
	_, d, err := workspaceRm(c, true)
	e := protoErr(t, err)
	if e.Code != protocol.WorkspaceFailed || !strings.Contains(e.Message, stateVol) ||
		!slices.Equal(d.verbs(), []string{"volume inspect", "ps --all", "volume rm"}) {
		t.Fatalf("%+v %q", e, d.calls)
	}
}

func TestWorkspaceRmOfAnotherVolumeLeavesStateAlone(t *testing.T) {
	d := clean().docker()
	if _, err := (Host{}).WorkspaceRmForce(context.Background(), Daemon{Runner: d}, "custom"); err != nil ||
		!slices.Equal(d.verbs(), []string{"volume inspect", "volume rm"}) {
		t.Fatalf("%v %q", err, d.calls)
	}
}

func TestWorkspaceRmDirty(t *testing.T) {
	c := clean()
	c.out = `{"dirty": ["publish", "src/o/a"]}` + "\n"
	_, d, err := workspaceRm(c, false)
	e := protoErr(t, err)
	want := "Workspace " + volume + " has uncommitted or unpushed work in: publish, src/o/a. Delete it anyway with ensemble-broker workspace-rm --volume " +
		volume + " --force."
	if e.Code != protocol.WorkspaceDirty || e.Message != want {
		t.Fatalf("%+v", e)
	}
	if slices.Contains(d.verbs(), "volume rm") {
		t.Fatal("dirty volume removed")
	}
}

func TestWorkspaceRmRejected(t *testing.T) {
	for name, labels := range map[string]string{
		"no labels":  "null",
		"no kind":    `{"ensemble.workspace":"` + workspace + `"}`,
		"empty kind": `{"ensemble.kind":"","ensemble.workspace":"` + workspace + `"}`,
	} {
		for _, force := range []bool{false, true} {
			_, d, err := workspaceRm(&check{labels: labels}, force)
			if protoErr(t, err).Code != protocol.WorkspaceRejected || len(d.calls) != 1 {
				t.Fatalf("%s force=%v: %v %q", name, force, err, d.calls)
			}
		}
	}
	for _, path := range []string{"", "relative", "/usr/lib/x", "/opt/ensemble", "/home/agent/x", "/", "/home", home + "/a/../b", home + "/a/", "/a\nb"} {
		c := clean()
		c.labels = labelled(strings.ReplaceAll(path, "\n", `\n`))
		_, d, err := workspaceRm(c, false)
		if protoErr(t, err).Code != protocol.WorkspaceRejected || len(d.calls) != 1 {
			t.Fatalf("path %q: %v %q", path, err, d.calls)
		}
	}
}

func TestWorkspaceRmFailed(t *testing.T) {
	type tc struct {
		edit  func(*check)
		verbs []string
	}
	failRun := &docker.CommandError{Exit: 1, Stderr: "boom"}
	for name, c := range map[string]tc{
		"no image":    {func(c *check) { c.images = "ensemble-worker:<none>\nensemble-worker:latest\n" }, []string{"volume inspect", "image ls"}},
		"run fails":   {func(c *check) { c.runErr = failRun }, []string{"volume inspect", "image ls", "image inspect", "run --rm", "rm --force"}},
		"bad output":  {func(c *check) { c.out = "Traceback\n" }, []string{"volume inspect", "image ls", "image inspect", "run --rm"}},
		"null dirty":  {func(c *check) { c.out = `{"dirty": null}` }, []string{"volume inspect", "image ls", "image inspect", "run --rm"}},
		"rm fails":    {func(c *check) { c.rmErr = &docker.CommandError{Exit: 1, Stderr: "volume is in use"} }, []string{"volume inspect", "image ls", "image inspect", "run --rm", "ps --all", "volume rm", "volume rm"}},
		"inspect err": {func(c *check) { c.inspect = &docker.CommandError{Exit: 1, Stderr: "boom"} }, []string{"volume inspect"}},
	} {
		t.Run(name, func(t *testing.T) {
			k := clean()
			c.edit(k)
			_, d, err := workspaceRm(k, false)
			e := protoErr(t, err)
			if e.Code != protocol.WorkspaceFailed || !slices.Equal(d.verbs(), c.verbs) {
				t.Fatalf("%v %q", err, d.calls)
			}
			if name == "run fails" && e.Cause != "boom" {
				t.Fatalf("cause %q", e.Cause)
			}
			if name == "run fails" && d.calls[4][2] != d.calls[3][3] {
				t.Fatalf("cleanup removed %q, not the check container", d.calls[4][2])
			}
		})
	}
	k := clean()
	k.inspect = unreachableErr
	if _, _, err := workspaceRm(k, false); protoErr(t, err).Code != protocol.DockerUnreachable {
		t.Fatal(err)
	}
}

func TestWorkspaceRmSameNameOnBothDaemons(t *testing.T) {
	remote, local := clean().docker(), clean().docker()
	h := onTwo(Host{}, identifiedRunner{testDaemon, remote}, identifiedRunner{localDaemon, local})
	got, err := h.WorkspaceRmForce(context.Background(), h.Daemons[1], volume)
	if err != nil || !got.Removed || len(remote.calls) != 0 {
		t.Fatalf("%+v %v; remote %q", got, err, remote.calls)
	}
	got, err = h.WorkspaceRmForce(context.Background(), h.Daemons[0], volume)
	if err != nil || !got.Removed || len(remote.calls) == 0 {
		t.Fatalf("target: %+v %v", got, err)
	}
}
