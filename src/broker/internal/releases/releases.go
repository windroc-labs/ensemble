// Package releases reads a client's latest release from the npm registry; it never downloads a package.
package releases

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"regexp"
	"time"

	"ensemble/broker/internal/npm"
)

var stableVersion = regexp.MustCompile(`^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$`)

type Registry struct {
	http *http.Client
}

// New queries the npm registry, each lookup within 30 s.
func New() Registry {
	return Registry{http: &http.Client{Timeout: 30 * time.Second}}
}

// Latest is pkg's latest dist-tag, a stable X.Y.Z. Errors name pkg, never the registry's response.
func (r Registry) Latest(ctx context.Context, pkg string) (string, error) {
	b, err := npm.Fetch(ctx, r.http, pkg, npm.Package(pkg, "latest"), npm.MaxMetadata)
	if err != nil {
		return "", err
	}
	var m struct {
		Name    string `json:"name"`
		Version string `json:"version"`
	}
	if json.Unmarshal(b, &m) != nil || m.Name != pkg || !stableVersion.MatchString(m.Version) {
		return "", errors.New("invalid latest " + pkg + " release")
	}
	return m.Version, nil
}
