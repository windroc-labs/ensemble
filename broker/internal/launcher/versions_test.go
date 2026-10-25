package launcher

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"os"
	"slices"
	"strings"
	"testing"

	"ensemble/broker/internal/codex"
)

type fakeVersions struct {
	calls                             []string
	pinned                            string
	target                            string
	verifyErr, registryErr, binaryErr error
	arch                              string
}

func (v *fakeVersions) Verify() (codex.Installation, error) {
	v.calls = append(v.calls, "verify")
	pinned := v.pinned
	if pinned == "" {
		pinned = "0.163.0"
	}
	return codex.Installation{Version: pinned}, v.verifyErr
}
func (v *fakeVersions) Latest(context.Context) (string, error) {
	v.calls = append(v.calls, "latest")
	return v.target, v.registryErr
}
func (v *fakeVersions) Linux(_ context.Context, _ string, arch string) ([]byte, error) {
	v.calls = append(v.calls, "linux")
	v.arch = arch
	return []byte("fake-native"), v.binaryErr
}

func defaultVersions() *fakeVersions {
	return &fakeVersions{target: "0.163.0"}
}

func TestClaudeStartsWithoutCodexVersionPreparation(t *testing.T) {
	for _, mode := range []string{"no_provider", "not_installed", "old_pinned", "registry_unavailable"} {
		t.Run(mode, func(t *testing.T) {
			h, d := startHost(&world{labels: goodLabels()}, &fakeCreds{})
			v := defaultVersions()
			switch mode {
			case "not_installed":
				v.verifyErr = codex.ErrNotInstalled
			case "old_pinned":
				v.pinned = "0.162.999"
			case "registry_unavailable":
				v.registryErr = errors.New("offline")
				v.binaryErr = errors.New("offline")
			}
			h.Versions = v
			if mode == "no_provider" {
				h.Versions = nil
			}
			h.Codex = &fakeCodexCreds{err: errors.New("Codex credentials must not be read")}
			var progress bytes.Buffer
			h.Progress = &progress
			got, err := h.Start(context.Background(), githubManifest(t))
			if err != nil {
				t.Fatal(err)
			}
			if len(v.calls) != 0 {
				t.Fatalf("Claude called Codex version runtime: %v", v.calls)
			}
			data, err := json.Marshal(got)
			if err != nil {
				t.Fatal(err)
			}
			var result map[string]any
			if err := json.Unmarshal(data, &result); err != nil {
				t.Fatal(err)
			}
			if _, present := result["client_update"]; present {
				t.Fatal("Claude response includes Codex audit")
			}
			if strings.Contains(progress.String(), "client_update") || strings.Contains(progress.String(), "Codex update") || strings.Contains(progress.String(), "update-codex") {
				t.Fatal(progress.String())
			}
			secrets := 0
			for _, call := range d.calls {
				if strings.Contains(strings.Join(call, " "), runtimeDir) || slices.Contains(call, updateScript) {
					t.Fatalf("Claude runtime preparation: %v", call)
				}
				if slices.Contains(call, "inject") || slices.Contains(call, "write") {
					secrets++
				}
			}
			if secrets != 2 {
				t.Fatalf("Claude/GitHub injection count %d", secrets)
			}
		})
	}
}

func TestCodexVersionLifecycle(t *testing.T) {
	for _, mode := range []string{"updated", "current", "registry_unavailable", "no_supported_release", "download_failed", "stage_failed", "publish_failed", "probe_failed", "wrapper_failed", "reset_failed", "restore_failed", "invalid_install"} {
		t.Run(mode, func(t *testing.T) {
			v := &fakeVersions{target: "0.164.0"}
			w := &world{labels: goodLabels(), versions: []string{"claude 2", "codex-cli 0.163.0", "1.19.3", "codex-cli 0.164.0", "codex-cli 0.164.0"}}
			want := mode
			switch mode {
			case "current":
				v.target = "0.163.0"
				w.versions = []string{"claude 2", "codex-cli 0.163.0", "1.19.3", "codex-cli 0.163.0"}
			case "registry_unavailable":
				v.registryErr = errors.New("offline")
				w.versions = []string{"claude 2", "codex-cli 0.163.0", "1.19.3", "codex-cli 0.163.0"}
			case "no_supported_release":
				v.target = "0.162.999"
				w.versions = []string{"claude 2", "codex-cli 0.163.0", "1.19.3", "codex-cli 0.163.0"}
			case "download_failed":
				v.binaryErr = errors.New("bad integrity")
				w.versions = []string{"claude 2", "codex-cli 0.163.0", "1.19.3", "codex-cli 0.163.0"}
				want = "update_failed"
			case "stage_failed", "publish_failed", "probe_failed", "wrapper_failed", "reset_failed", "restore_failed":
				want = "update_failed"
				w.versions = []string{"claude 2", "codex-cli 0.163.0", "1.19.3", "codex-cli 0.163.0", "codex-cli 0.163.0"}
				if mode == "publish_failed" || mode == "wrapper_failed" {
					w.versions = []string{"claude 2", "codex-cli 0.163.0", "1.19.3", "codex-cli 0.164.0", "codex-cli 0.163.0", "codex-cli 0.163.0"}
				}
				w.execErr = func(args []string) error {
					if (mode != "probe_failed" && slices.Contains(args, strings.TrimSuffix(mode, "_failed"))) || (mode == "reset_failed" && slices.Contains(args, "stage")) || (mode == "restore_failed" && slices.Contains(args, "stage")) {
						return errors.New("failed")
					}
					if mode == "restore_failed" && slices.Contains(args, "--version") && slices.Contains(args, "/opt/ensemble/bin/codex") {
						return errors.New("restore failed")
					}
					return nil
				}
			case "invalid_install":
				v.verifyErr = errors.New("untrusted")
			}
			h, d := startHost(w, &fakeCreds{})
			h.Versions = v
			var progress bytes.Buffer
			h.Progress = &progress
			h.Codex = &fakeCodexCreds{}
			got, err := h.Start(context.Background(), codexManifest(t))
			blocked := slices.Contains([]string{"reset_failed", "restore_failed", "invalid_install"}, mode)
			if (err != nil) != blocked {
				t.Fatalf("%+v %v", got, err)
			}
			injected := false
			for _, call := range d.calls {
				if slices.Contains(call, "inject-codex") {
					injected = true
				}
			}
			if injected == blocked {
				t.Fatalf("secret ordering: %v %v", injected, err)
			}
			if blocked {
				return
			}
			index := func(arg string) int {
				return slices.IndexFunc(d.calls, func(call []string) bool { return slices.Contains(call, arg) })
			}
			stage, stagedProbe, publish, reset, firstPublic, inject := index("stage"), index(runtimeDir+"/codex.next/bin/codex"), index("publish"), index("reset"), index("/opt/ensemble/bin/codex"), index("inject-codex")
			lastPublic, publicCount := -1, 0
			for n, call := range d.calls {
				if slices.Contains(call, "/opt/ensemble/bin/codex") && slices.Contains(call, "--version") {
					lastPublic = n
					publicCount++
				}
			}
			if want == "update_failed" && !(reset >= 0 && reset < lastPublic && lastPublic < inject) {
				t.Fatalf("fallback order: %v", d.calls)
			}
			switch mode {
			case "publish_failed":
				if !(stage >= 0 && stage < stagedProbe && stagedProbe < publish && publish < reset && publicCount == 1) {
					t.Fatalf("publish failure not reached: %v", d.calls)
				}
			case "wrapper_failed":
				if !(stage >= 0 && stage < stagedProbe && stagedProbe < publish && publish < firstPublic && firstPublic < reset && publicCount == 2) {
					t.Fatalf("published probe failure not reached: %v", d.calls)
				}
			case "probe_failed":
				if !(stage >= 0 && stage < stagedProbe && stagedProbe < reset && publish == -1) {
					t.Fatalf("staged probe failure not reached: %v", d.calls)
				}
			}
			if got.ClientUpdate.Result != want || got.ClientUpdate.PinnedVersion != "0.163.0" || got.ClientUpdate.ImageVersion != "0.163.0" || got.ClientUpdate.MinVersion != "0.163.0" {
				t.Fatalf("%+v", got.ClientUpdate)
			}
			expected := "codex-cli 0.163.0"
			if mode == "updated" {
				expected = "codex-cli 0.164.0"
				if v.arch != "arm64" {
					t.Fatal(v.arch)
				}
			}
			if got.Clients.Codex != expected {
				t.Fatalf("actual %s", got.Clients.Codex)
			}
			if !strings.Contains(progress.String(), "Codex update "+want) {
				t.Fatal(progress.String())
			}
			if v.target == "0.164.0" && mode != "registry_unavailable" && !strings.Contains(progress.String(), "sudo /usr/local/libexec/ensemble/ensemble-broker update-codex 0.164.0") {
				t.Fatal("missing upgrade hint")
			}
			var run []string
			for _, call := range d.calls {
				if len(call) > 1 && call[0] == "run" && call[1] == "--detach" {
					run = call
				}
			}
			if !strings.Contains(strings.Join(run, " "), "/opt/ensemble/runtime:rw,exec,nosuid,nodev,") {
				t.Fatal("missing volatile executable mount")
			}
		})
	}
}

func TestCompanionProbeFailureFallsBackBeforeSecrets(t *testing.T) {
	for _, path := range []string{runtimeDir + "/codex.next/bin/codex", "/opt/ensemble/bin/codex"} {
		t.Run(path, func(t *testing.T) {
			failed := false
			w := &world{labels: goodLabels(), versions: []string{"claude 2", "codex-cli 0.163.0", "1.19.3", "codex-cli 0.164.0", "codex-cli 0.164.0", "codex-cli 0.163.0"}}
			if strings.Contains(path, "codex.next") {
				w.versions = []string{"claude 2", "codex-cli 0.163.0", "1.19.3", "codex-cli 0.164.0", "codex-cli 0.163.0"}
			}
			w.execErr = func(args []string) error {
				if !failed && slices.Contains(args, "probe") && slices.Contains(args, path) {
					failed = true
					return errors.New("companion cannot start")
				}
				return nil
			}
			h, d := startHost(w, &fakeCreds{})
			h.Versions = &fakeVersions{target: "0.164.0"}
			h.Codex = &fakeCodexCreds{}
			got, err := h.Start(context.Background(), codexManifest(t))
			if err != nil || !failed || got.ClientUpdate.Result != "update_failed" || got.Clients.Codex != "codex-cli 0.163.0" {
				t.Fatalf("fallback failed: %+v %v", got, err)
			}
			reset, restored := false, false
			for _, call := range d.calls {
				if slices.Contains(call, "reset") {
					reset = true
				}
				if reset && slices.Contains(call, "probe") && slices.Contains(call, "0.163.0") {
					restored = true
				}
				if slices.Contains(call, "inject-codex") && !restored {
					t.Fatal("credentials injected before restored companion probe")
				}
			}
		})
	}
}

func TestCodexUpdateBeforeAllSecrets(t *testing.T) {
	for _, blocked := range []bool{false, true} {
		t.Run("codex"+map[bool]string{true: "-rejected"}[blocked], func(t *testing.T) {
			v := &fakeVersions{target: "0.164.0"}
			if blocked {
				v.pinned = "0.162.999"
			}
			h, d := startHost(&world{labels: goodLabels(), versions: []string{"claude 2", "codex-cli 0.163.0", "1.19.3", "codex-cli 0.164.0", "codex-cli 0.164.0"}}, &fakeCreds{})
			h.Versions = v
			h.Codex = &fakeCodexCreds{}
			b, _ := os.ReadFile(codexManifest(t))
			var m map[string]any
			if err := json.Unmarshal(b, &m); err != nil {
				t.Fatal(err)
			}
			m["github"] = map[string]any{"app": "ensemble-bot", "permissions": fixtureSet}
			b, _ = json.Marshal(m)
			manifest := writeManifest(t, b)
			_, err := h.Start(context.Background(), manifest)
			if (err != nil) != blocked {
				t.Fatal(err)
			}
			stage, publish, probe, secrets := -1, -1, -1, 0
			for i, call := range d.calls {
				if slices.Contains(call, "stage") {
					stage = i
				}
				if slices.Contains(call, "publish") {
					publish = i
				}
				if slices.Contains(call, "--version") {
					probe = i
				}
				if slices.Contains(call, "inject") || slices.Contains(call, "inject-codex") || slices.Contains(call, "write") {
					secrets++
					if blocked || !(stage < publish && publish < probe && probe < i) {
						t.Fatalf("secret before verified update: %v", d.calls)
					}
				}
			}
			if (!blocked && secrets != 2) || (blocked && secrets != 0) {
				t.Fatal(secrets)
			}
		})
	}
}

func TestPinnedMinimumVersion(t *testing.T) {
	for _, version := range []string{"0.162.999", "0.163.0", "0.164.0", "invalid"} {
		t.Run(version, func(t *testing.T) {
			v := defaultVersions()
			v.pinned = version
			h := Host{Versions: v}
			_, err := h.versionPolicy(context.Background())
			blocked := version == "0.162.999" || version == "invalid"
			if (err != nil) != blocked {
				t.Fatalf("version=%s error=%v", version, err)
			}
			if blocked && !strings.Contains(err.Error(), "update-codex 0.163.0") {
				t.Fatal(err)
			}
			if blocked && slices.Contains(v.calls, "latest") {
				t.Fatal("registry queried before pinned validation")
			}
		})
	}
}

func TestImageMinimumVersion(t *testing.T) {
	for _, version := range []string{"0.162.999", "0.163.0", "0.164.0", "invalid"} {
		t.Run(version, func(t *testing.T) {
			v := defaultVersions()
			v.target = version
			h, _ := startHost(&world{labels: goodLabels(), versions: []string{"codex-cli " + version}}, &fakeCreds{})
			h.Versions = v
			_, _, err := h.updateCodex(context.Background(), "container", "arm64", Clients{Codex: "codex-cli " + version}, codex.Installation{Version: "0.163.0"})
			blocked := version == "0.162.999" || version == "invalid"
			if (err != nil) != blocked {
				t.Fatalf("version=%s error=%v", version, err)
			}
			if blocked && !strings.Contains(err.Error(), "rebuild") {
				t.Fatal(err)
			}
			if blocked && len(v.calls) != 0 {
				t.Fatal("registry queried before image validation")
			}
		})
	}
}

func TestCurrentImageRequiresCompanionBeforeSecrets(t *testing.T) {
	w := &world{labels: goodLabels(), execErr: failOn("probe", errors.New("image companion missing"))}
	h, d := startHost(w, &fakeCreds{})
	h.Versions = defaultVersions()
	h.Codex = &fakeCodexCreds{}
	if _, err := h.Start(context.Background(), codexManifest(t)); err == nil {
		t.Fatal("current image accepted without its companion")
	}
	for _, call := range d.calls {
		if slices.Contains(call, "inject-codex") {
			t.Fatal("credentials injected before image companion verification")
		}
	}
}
