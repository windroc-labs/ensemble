// Package releases reads a client's latest release from the npm registry; it never downloads a package.
package releases

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"regexp"
	"strings"
	"time"
)

const (
	registry    = "https://registry.npmjs.org"
	maxMetadata = 1 << 20
)

var stableVersion = regexp.MustCompile(`^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$`)

type Registry struct {
	base string
	http *http.Client
}

// New queries registry.npmjs.org, each lookup within 30 s, following no redirect.
func New() Registry {
	return Registry{base: registry, http: &http.Client{
		Timeout:       30 * time.Second,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}}
}

// Latest is pkg's latest dist-tag, a stable X.Y.Z. Errors name pkg, never the registry's response.
func (r Registry) Latest(ctx context.Context, pkg string) (string, error) {
	unavailable := errors.New(pkg + " registry unavailable")
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, r.base+"/"+strings.ReplaceAll(pkg, "/", "%2F")+"/latest", nil)
	if err != nil {
		return "", errors.New("invalid " + pkg + " registry request")
	}
	req.Header.Set("User-Agent", "ensemble-broker")
	resp, err := r.http.Do(req)
	if err != nil {
		return "", unavailable
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return "", unavailable
	}
	b, err := io.ReadAll(io.LimitReader(resp.Body, maxMetadata+1))
	if err != nil || len(b) > maxMetadata {
		return "", unavailable
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
