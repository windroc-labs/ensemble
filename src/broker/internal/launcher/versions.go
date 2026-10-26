package launcher

import (
	"strings"

	"ensemble/broker/internal/codex"
	"ensemble/broker/internal/protocol"
)

// opencodeMinVersion is the lowest OpenCode a build installs and an opencode start accepts in the image.
const opencodeMinVersion = "1.19.3"

type CodexVersions interface {
	Verify() (codex.Installation, error)
}

func (h Host) versionPolicy() error {
	if h.Versions == nil {
		return protocol.New(protocol.StartFailed, "Codex version policy unavailable; reinstall the broker.")
	}
	i, err := h.Versions.Verify()
	if err != nil {
		return protocol.New(protocol.StartFailed, "Pinned Codex unavailable; run sudo /usr/local/libexec/ensemble/ensemble-broker install-codex <version> or update-codex <version>.")
	}
	if !codex.SupportedVersion(i.Version) {
		return protocol.Newf(protocol.StartFailed, "Pinned Codex requires version %s or newer; run sudo /usr/local/libexec/ensemble/ensemble-broker update-codex %s.", codex.MinVersion, codex.MinVersion)
	}
	return nil
}

// checkCodex requires the image's codex --version output to be codex-cli and a version of at least codex.MinVersion.
func checkCodex(version string) error {
	if v, ok := strings.CutPrefix(version, "codex-cli "); !ok || !codex.SupportedVersion(v) {
		return protocol.New(protocol.StartFailed, "Image Codex version is invalid or below the minimum; rebuild the worker image with Codex "+codex.MinVersion+" or newer.")
	}
	return nil
}

// checkOpencode requires the image's opencode --version output to be a stable version of at least opencodeMinVersion.
func checkOpencode(version string) error {
	if !codex.ValidVersion(version) || codex.Newer(opencodeMinVersion, version) {
		return protocol.New(protocol.StartFailed, "Image OpenCode version is invalid or below the minimum; rebuild the worker image with OpenCode "+opencodeMinVersion+" or newer.")
	}
	return nil
}
