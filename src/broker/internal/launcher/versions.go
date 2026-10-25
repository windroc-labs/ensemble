package launcher

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	"ensemble/broker/internal/codex"
	"ensemble/broker/internal/protocol"
)

const runtimeDir = "/opt/ensemble/runtime"
const updateScript = "/opt/ensemble/lib/codex-update.py"

// opencodeMinVersion is the lowest image OpenCode an opencode start accepts; the image pins it (assets/opencode-install.py).
const opencodeMinVersion = "1.19.3"

type CodexVersions interface {
	Verify() (codex.Installation, error)
	Latest(context.Context) (string, error)
	Linux(context.Context, string, string) ([]byte, error)
}

type ClientUpdate struct {
	Client        string `json:"client"`
	PinnedVersion string `json:"pinned_version"`
	MinVersion    string `json:"min_version"`
	ImageVersion  string `json:"image_version"`
	TargetVersion string `json:"target_version"`
	Result        string `json:"result"`
}

func (h Host) versionPolicy(ctx context.Context) (codex.Installation, error) {
	if h.Versions == nil {
		return codex.Installation{}, protocol.New(protocol.StartFailed, "Codex version policy unavailable; reinstall the broker.")
	}
	i, err := h.Versions.Verify()
	if err != nil {
		return i, protocol.New(protocol.StartFailed, "Pinned Codex unavailable; run sudo /usr/local/libexec/ensemble/ensemble-broker install-codex <version> or update-codex <version>.")
	}
	if !codex.SupportedVersion(i.Version) {
		return i, protocol.Newf(protocol.StartFailed, "Pinned Codex requires version %s or newer; run sudo /usr/local/libexec/ensemble/ensemble-broker update-codex %s.", codex.MinVersion, codex.MinVersion)
	}
	hintCtx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	if latest, err := h.Versions.Latest(hintCtx); err == nil && codex.SupportedVersion(latest) && codex.Newer(latest, i.Version) && h.Progress != nil {
		fmt.Fprintf(h.Progress, "ensemble-broker: newer pinned Codex available; run sudo /usr/local/libexec/ensemble/ensemble-broker update-codex %s\n", latest)
	}
	return i, nil
}

// checkOpencode requires the image's opencode --version output to be a stable version of at least opencodeMinVersion.
func checkOpencode(version string) error {
	if !codex.ValidVersion(version) || codex.Newer(opencodeMinVersion, version) {
		return protocol.New(protocol.StartFailed, "Image OpenCode version is invalid or below the minimum; rebuild the worker image with OpenCode "+opencodeMinVersion+" or newer.")
	}
	return nil
}

func (h Host) updateCommand(ctx context.Context, c, action string, stdin []byte) error {
	_, err := h.Docker.Run(ctx, []string{"exec", "--interactive", "--user", "root", c, Python, "-I", updateScript, action}, stdin)
	return err
}

func (h Host) codexProbe(ctx context.Context, c, path, version string) error {
	out, err := h.Docker.Run(ctx, asAgent(c, path, "--version"), nil)
	if err != nil {
		return err
	}
	if strings.TrimSpace(string(out)) != "codex-cli "+version {
		return errors.New("Codex version mismatch")
	}
	_, err = h.Docker.Run(ctx, asAgent(c, Python, "-I", updateScript, "probe", path, version), nil)
	return err
}

func (h Host) updateCodex(ctx context.Context, c, arch string, clients Clients, i codex.Installation) (Clients, ClientUpdate, error) {
	image := strings.TrimPrefix(clients.Codex, "codex-cli ")
	u := ClientUpdate{Client: "codex", PinnedVersion: i.Version, MinVersion: codex.MinVersion, ImageVersion: image}
	if clients.Codex != "codex-cli "+image || !codex.SupportedVersion(image) {
		return clients, u, protocol.New(protocol.StartFailed, "Image Codex version is invalid or below the minimum; rebuild the worker image with Codex "+codex.MinVersion+" or newer.")
	}
	updateCtx, cancel := context.WithTimeout(ctx, 2*time.Minute)
	defer cancel()
	target, err := h.Versions.Latest(updateCtx)
	u.TargetVersion = target
	switch {
	case err != nil:
		u.TargetVersion = ""
		u.Result = "registry_unavailable"
	case !codex.SupportedVersion(target):
		u.TargetVersion = ""
		u.Result = "no_supported_release"
	case target == image:
		u.Result = "current"
		if err := h.codexProbe(updateCtx, c, "/opt/ensemble/bin/codex", image); err != nil {
			return clients, u, failure(err, protocol.StartFailed, "Cannot verify image Codex.")
		}
	default:
		u.Result = "update_failed"
		native, downloadErr := h.Versions.Linux(updateCtx, target, arch)
		if downloadErr == nil {
			err = h.updateCommand(updateCtx, c, "stage", native)
			if err == nil {
				err = h.codexProbe(updateCtx, c, runtimeDir+"/codex.next/bin/codex", target)
			}
			if err == nil {
				err = h.updateCommand(updateCtx, c, "publish", nil)
			}
			if err == nil {
				err = h.codexProbe(updateCtx, c, "/opt/ensemble/bin/codex", target)
			}
			if err == nil {
				u.Result = "updated"
				clients.Codex = "codex-cli " + target
			}
		}
	}
	if u.Result != "updated" && u.Result != "current" {
		restoreCtx, restoreCancel := context.WithTimeout(context.WithoutCancel(ctx), 30*time.Second)
		defer restoreCancel()
		if err := h.updateCommand(restoreCtx, c, "reset", nil); err != nil {
			return clients, u, failure(err, protocol.StartFailed, "Cannot restore image Codex after update failure.")
		}
		if err := h.codexProbe(restoreCtx, c, "/opt/ensemble/bin/codex", image); err != nil {
			return clients, u, failure(err, protocol.StartFailed, "Cannot verify restored image Codex.")
		}
	}
	if err := ctx.Err(); err != nil {
		return clients, u, protocol.New(protocol.StartFailed, "Codex version preparation canceled.")
	}
	if h.Progress != nil {
		fmt.Fprintf(h.Progress, "ensemble-broker: Codex update %s; image=%s actual=%s\n", u.Result, image, strings.TrimPrefix(clients.Codex, "codex-cli "))
	}
	return clients, u, nil
}
