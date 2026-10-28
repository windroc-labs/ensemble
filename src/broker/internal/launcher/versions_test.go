package launcher

import (
	"bytes"
	"context"
	"errors"
	"slices"
	"strings"
	"testing"

	"ensemble/broker/internal/codex"
	"ensemble/broker/internal/protocol"
)

type fakeVersions struct {
	calls     []string
	pinned    string
	verifyErr error
}

func (v *fakeVersions) Verify() (codex.Installation, error) {
	v.calls = append(v.calls, "verify")
	pinned := v.pinned
	if pinned == "" {
		pinned = "0.163.0"
	}
	return codex.Installation{Version: pinned}, v.verifyErr
}

func defaultVersions() *fakeVersions {
	return &fakeVersions{}
}

func TestClaudeStartsWithoutCodexVersionPreparation(t *testing.T) {
	for _, mode := range []string{"no_provider", "not_installed", "old_pinned"} {
		t.Run(mode, func(t *testing.T) {
			h, d := startHost(&world{labels: goodLabels()}, &fakeCreds{})
			v := defaultVersions()
			switch mode {
			case "not_installed":
				v.verifyErr = codex.ErrNotInstalled
			case "old_pinned":
				v.pinned = "0.162.999"
			}
			h.Versions = v
			if mode == "no_provider" {
				h.Versions = nil
			}
			h.Codex = &fakeCodexCreds{err: errors.New("Codex credentials must not be read")}
			var progress bytes.Buffer
			h.Progress = &progress
			if _, err := h.Start(context.Background(), githubManifest(t)); err != nil {
				t.Fatal(err)
			}
			if len(v.calls) != 0 {
				t.Fatalf("Claude called Codex version runtime: %v", v.calls)
			}
			if strings.Contains(progress.String(), "update-codex") {
				t.Fatal(progress.String())
			}
			secrets := 0
			for _, call := range d.calls {
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

func TestPinnedMinimumVersion(t *testing.T) {
	for _, version := range []string{"0.162.999", "0.163.0", "0.164.0", "invalid"} {
		t.Run(version, func(t *testing.T) {
			v := defaultVersions()
			v.pinned = version
			h := Host{Versions: v}
			err := h.versionPolicy()
			blocked := version == "0.162.999" || version == "invalid"
			if (err != nil) != blocked {
				t.Fatalf("version=%s error=%v", version, err)
			}
			if blocked && !strings.Contains(err.Error(), "update-codex 0.163.0") {
				t.Fatal(err)
			}
		})
	}
}

func TestVersionPolicyMakesNoLookup(t *testing.T) {
	for _, mode := range []string{"newer_pinned", "not_installed", "no_provider"} {
		t.Run(mode, func(t *testing.T) {
			v := &fakeVersions{pinned: "0.164.0"}
			var progress bytes.Buffer
			h := Host{Versions: v, Progress: &progress}
			switch mode {
			case "not_installed":
				v.verifyErr = codex.ErrNotInstalled
			case "no_provider":
				h.Versions = nil
			}
			err := h.versionPolicy()
			if (err != nil) != (mode != "newer_pinned") {
				t.Fatal(err)
			}
			if err != nil && protoErr(t, err).Code != protocol.StartFailed {
				t.Fatal(err)
			}
			want := []string{"verify"}
			if mode == "no_provider" {
				want = nil
			}
			if !slices.Equal(v.calls, want) || progress.Len() != 0 {
				t.Fatalf("calls %v, progress %q", v.calls, progress.String())
			}
		})
	}
}

func TestCheckCodex(t *testing.T) {
	for _, c := range []struct {
		version string
		ok      bool
	}{
		{"codex-cli 0.163.0", true}, {"codex-cli 0.164.0", true}, {"codex-cli 1.0.0", true},
		{"codex-cli 0.162.999", false},
		{"0.163.0", false}, {"codex-cli 0.163.0-beta", false}, {"codex-cli  0.163.0", false}, {"codex 0.163.0", false}, {"", false},
	} {
		t.Run(c.version, func(t *testing.T) {
			err := checkCodex(c.version)
			if (err == nil) != c.ok {
				t.Fatalf("%q: %v", c.version, err)
			}
			if err == nil {
				return
			}
			e := protoErr(t, err)
			want := "Image Codex version is invalid or below the minimum; rebuild the worker image with Codex " + codex.MinVersion + " or newer."
			if e.Code != protocol.StartFailed || e.Message != want {
				t.Fatalf("%+v", e)
			}
		})
	}
}

func TestCodexImageVersionCheckedBeforeSecrets(t *testing.T) {
	for _, version := range []string{"codex-cli 0.162.999", "codex-cli bad"} {
		t.Run(version, func(t *testing.T) {
			h, d := startHost(&world{labels: labelsWithVersion("codex", strings.TrimPrefix(version, "codex-cli "))}, &fakeCreds{})
			h.Codex = &fakeCodexCreds{}
			_, err := h.Start(context.Background(), codexManifest(t))
			if e := protoErr(t, err); e.Code != protocol.StartFailed || !strings.Contains(e.Message, "rebuild the worker image") {
				t.Fatalf("%+v", e)
			}
			for _, call := range d.calls {
				if slices.Contains(call, "inject-codex") {
					t.Fatal("credentials injected before the image Codex check")
				}
			}
		})
	}
}
