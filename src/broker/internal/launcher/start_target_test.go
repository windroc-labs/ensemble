package launcher

import (
	"context"
	"errors"
	"strings"
	"testing"

	"ensemble/broker/internal/protocol"
)

func TestManifestRejectsTargetFields(t *testing.T) {
	for _, field := range []string{"docker_host", "endpoint", "target"} {
		data := fixtureBytes(t, "manifest-valid", func(m map[string]any) { m[field] = "ssh://u@elsewhere" })
		_, err := ParseManifest(data)
		if e := protoErr(t, err); e.Code != protocol.ManifestRejected || !strings.Contains(e.Message, "unknown field") {
			t.Fatalf("%s: %+v", field, e)
		}
	}
}

func TestStartStopsAtAnUnreachableTarget(t *testing.T) {
	creds := &fakeCreds{}
	h, d := startHost(&world{labels: goodLabels(), unreachable: true}, creds)
	_, err := h.Start(context.Background(), validManifest(t))
	e := protoErr(t, err)
	if e.Code != protocol.DockerUnreachable || e.Message != "Cannot reach Docker target ssh://u@pc: host key mismatch." {
		t.Fatalf("%+v", e)
	}
	if len(d.calls) != 1 {
		t.Fatalf("docker calls after the unreachable target: %q", d.verbs())
	}
}

func TestNoWorkerMountReachesTheTargetFileOrSSHDir(t *testing.T) {
	for _, fixture := range []string{"manifest-valid", "manifest-codex"} {
		for _, cwd := range []string{home + "/.ensemble/docker", home + "/.ssh"} {
			data := fixtureBytes(t, fixture, func(m map[string]any) { m["cwd"] = cwd })
			m, err := ParseManifest(data)
			if err != nil {
				t.Fatal(err)
			}
			if err := mountHost().checkMounts(m); err != nil {
				t.Fatalf("%s %s: %v", fixture, cwd, err)
			}
			flags, _ := mounts(m, goodLabels())
			for i := 0; i+1 < len(flags); i += 2 {
				if flags[i] != "--mount" {
					continue
				}
				spec := flags[i+1]
				switch {
				case strings.HasPrefix(spec, "type=volume,") && strings.Contains(spec, ",target="+cwd+","):
				case strings.HasPrefix(spec, "type=volume,source=ensemble-state-"+m.Name+","):
				default:
					t.Fatalf("%s %s: mount %s", fixture, cwd, spec)
				}
			}
		}
	}
}

func TestStartDaemonLookupFailureCreatesNoVolume(t *testing.T) {
	for _, tc := range []struct {
		name, out string
		err       error
		code      protocol.Code
	}{
		{"unreachable", "", unreachableErr, protocol.DockerUnreachable},
		{"unusable", `"../x"`, nil, protocol.StartFailed},
	} {
		t.Run(tc.name, func(t *testing.T) {
			w := &world{labels: goodLabels(), daemon: tc.out, daemonErr: tc.err}
			if tc.out == "" {
				w.daemon = " "
			}
			h, d := startHost(w, &fakeCreds{})
			_, err := h.Start(context.Background(), validManifest(t))
			var pe *protocol.Error
			if !errors.As(err, &pe) || pe.Code != tc.code {
				t.Fatalf("%v, want %s", err, tc.code)
			}
			for _, c := range d.calls {
				if c[0] == "volume" || c[0] == "run" {
					t.Fatalf("%q after a failed daemon lookup", c)
				}
			}
		})
	}
}

// The refusal hint follows whether the daemon is remote, never the shape of its endpoint.
func TestEgressHintFollowsDaemonRemote(t *testing.T) {
	const notSSH = "tcp://pc:2376"
	for _, tc := range []struct {
		endpoint string
		remote   bool
		hint     string
	}{
		{notSSH, true, " Install it on the Docker target " + notSSH + " by running bash <ensemble plugin directory>/scripts/install-target-egress.sh on this Mac, wait until it is healthy, then retry."},
		{remoteTarget, false, localHint},
	} {
		none := ""
		w := world{proxies: &none, labels: goodLabels()}
		h, _, _, _, _, _, _ := credentialHost(&w)
		h.Daemons = []Daemon{{Name: DaemonRemote, Endpoint: tc.endpoint, Remote: tc.remote, Runner: h.Daemons[0].Runner}}
		_, err := h.Start(context.Background(), validManifest(t))
		if e := protoErr(t, err); e.Code != protocol.EgressUnavailable || !strings.HasSuffix(e.Message, tc.hint) {
			t.Errorf("%s remote %v: %+v", tc.endpoint, tc.remote, e)
		}
	}
}
