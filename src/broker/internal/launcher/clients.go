package launcher

import (
	"context"
	"errors"
	"time"

	"ensemble/broker/internal/codex"
)

// claudeMinVersion is the lowest Claude Code a build installs.
const claudeMinVersion = "2.1.0"

// Releases is the npm registry's side of a build: a client package's latest stable release.
type Releases interface {
	Latest(ctx context.Context, pkg string) (string, error)
}

// ClientVersions are the client versions an image carries in its labels, each an exact stable X.Y.Z.
type ClientVersions struct{ Claude, Codex, Opencode string }

// clientPackages are the image's clients, in ClientVersions' field order.
var clientPackages = [...]struct{ name, pkg, label, min string }{
	{"claude", "@anthropic-ai/claude-code", "ensemble.claude-version", claudeMinVersion},
	{"codex", "@openai/codex", "ensemble.codex-version", codex.MinVersion},
	{"opencode", "opencode-ai", "ensemble.opencode-version", opencodeMinVersion},
}

func (v *ClientVersions) fields() [len(clientPackages)]*string {
	return [...]*string{&v.Claude, &v.Codex, &v.Opencode}
}

// latestClients looks up every client's latest release within 2 minutes; each must be at least its minimum.
func (h Host) latestClients(ctx context.Context) (ClientVersions, error) {
	if h.Releases == nil {
		return ClientVersions{}, errors.New("no client release lookup")
	}
	ctx, cancel := context.WithTimeout(ctx, 2*time.Minute)
	defer cancel()
	var v ClientVersions
	for i, field := range v.fields() {
		c := clientPackages[i]
		latest, err := h.Releases.Latest(ctx, c.pkg)
		if err != nil {
			return ClientVersions{}, err
		}
		if !codex.ValidVersion(latest) {
			return ClientVersions{}, errors.New("invalid latest " + c.pkg + " release")
		}
		if codex.Newer(c.min, latest) {
			return ClientVersions{}, errors.New("latest " + c.pkg + " release " + latest + " is below " + c.min)
		}
		*field = latest
	}
	return v, nil
}

// imageClients reads an image's client versions from its labels; ok only when all three are valid.
func imageClients(labels map[string]string) (ClientVersions, bool) {
	var v ClientVersions
	for i, field := range v.fields() {
		*field = labels[clientPackages[i].label]
		if !codex.ValidVersion(*field) {
			return ClientVersions{}, false
		}
	}
	return v, true
}

func (v ClientVersions) differs(latest ClientVersions) bool {
	return v != latest
}
