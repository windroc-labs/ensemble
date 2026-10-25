package codex

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
)

const MinVersion = "0.163.0"

func SupportedVersion(version string) bool {
	return ValidVersion(version) && !Newer(MinVersion, version)
}

var errReleaseMissing = errors.New("Codex registry release missing")

func Newer(a, b string) bool {
	if !ValidVersion(a) || !ValidVersion(b) {
		return false
	}
	x, y := strings.Split(a, "."), strings.Split(b, ".")
	for i := range x {
		if len(x[i]) != len(y[i]) {
			return len(x[i]) > len(y[i])
		}
		if x[i] != y[i] {
			return x[i] > y[i]
		}
	}
	return false
}

func (r Runtime) Latest(ctx context.Context) (string, error) {
	b, err := r.download(ctx, registry+"/@openai%2Fcodex/latest", maxMetadata)
	if err != nil {
		return "", err
	}
	var m packageMetadata
	if json.Unmarshal(b, &m) != nil || m.Name != "@openai/codex" || !ValidVersion(m.Version) {
		return "", errors.New("invalid latest Codex release")
	}
	return m.Version, nil
}

func (r Runtime) Linux(ctx context.Context, version, arch string) ([]byte, error) {
	cpu, triple := "arm64", "aarch64-unknown-linux-musl"
	switch arch {
	case "arm64":
	case "amd64":
		cpu, triple = "x64", "x86_64-unknown-linux-musl"
	default:
		return nil, errors.New("unsupported Codex container architecture")
	}
	if !ValidVersion(version) {
		return nil, errors.New("Codex requires an exact stable version")
	}
	b, err := r.platformArchive(ctx, version, "linux", cpu)
	if err != nil {
		return nil, err
	}
	for _, name := range []string{"codex", "codex-code-mode-host"} {
		if _, err := readEntryBounded(b, "package/vendor/"+triple+"/bin/"+name, maxExpanded); err != nil {
			return nil, err
		}
	}
	return b, nil
}
