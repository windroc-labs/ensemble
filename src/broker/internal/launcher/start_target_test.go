package launcher

import (
	"context"
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
