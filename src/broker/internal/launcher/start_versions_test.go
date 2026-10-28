package launcher

import (
	"context"
	"slices"
	"testing"
)

func labelsWithVersion(client, version string) map[string]string {
	labels := goodLabels()
	labels["ensemble."+client+"-version"] = version
	return labels
}

func TestStartReadsVersionLabels(t *testing.T) {
	labels := goodLabels()
	labels["ensemble.claude-version"] = "2.9.8"
	labels["ensemble.codex-version"] = "0.170.0"
	labels["ensemble.opencode-version"] = "1.20.1"
	h, d := startHost(&world{labels: labels}, &fakeCreds{})
	got, err := h.Start(context.Background(), validManifest(t))
	want := Clients{Claude: "2.9.8 (Claude Code)", Codex: "codex-cli 0.170.0", Opencode: "1.20.1"}
	if err != nil || got.Clients != want {
		t.Fatalf("clients %+v, error %v", got.Clients, err)
	}
	for _, call := range d.calls {
		if slices.Contains(call, "--version") {
			t.Fatalf("version exec: %q", call)
		}
	}
}

func TestStartRejectsInvalidVersionLabels(t *testing.T) {
	h, _ := startHost(&world{labels: labelsWithVersion("codex", "")}, &fakeCreds{})
	_, err := h.Start(context.Background(), validManifest(t))
	if err == nil || protoErr(t, err).Message != "Image client version labels are missing or invalid; rebuild the worker image with ensemble-broker build." {
		t.Fatal(err)
	}
}
