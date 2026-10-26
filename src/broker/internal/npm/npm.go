// Package npm fetches from the npm registry, registry.npmjs.org only.
package npm

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/url"
	"strings"
)

const (
	registry    = "https://registry.npmjs.org"
	MaxMetadata = 1 << 20
)

// Allowed reports whether raw is an https registry.npmjs.org URL whose path starts with "/", with no userinfo, query,
// fragment or opaque part.
func Allowed(raw string) bool {
	u, err := url.Parse(raw)
	return err == nil && u.Scheme+"://"+u.Host == registry && u.User == nil && u.RawQuery == "" && u.Fragment == "" && u.Opaque == "" && strings.HasPrefix(u.Path, "/")
}

// Package is the URL of name at version (a version or a dist-tag).
func Package(name, version string) string {
	return registry + "/" + strings.ReplaceAll(name, "/", "%2F") + "/" + version
}

// Fetch GETs raw through client, following no redirect, and returns a body of at most limit bytes.
// Errors name subject, never the registry's response.
func Fetch(ctx context.Context, client *http.Client, subject, raw string, limit int64) ([]byte, error) {
	if !Allowed(raw) {
		return nil, errors.New(subject + " download origin rejected")
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, raw, nil)
	if err != nil {
		return nil, errors.New("invalid " + subject + " registry request")
	}
	req.Header.Set("User-Agent", "ensemble-broker")
	c := *client
	c.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	unavailable := errors.New(subject + " registry unavailable")
	resp, err := c.Do(req)
	if err != nil {
		return nil, unavailable
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNotFound {
		return nil, errors.New(subject + " registry release missing")
	}
	if resp.StatusCode != http.StatusOK || resp.ContentLength > limit {
		return nil, unavailable
	}
	b, err := io.ReadAll(io.LimitReader(resp.Body, limit+1))
	if err != nil || int64(len(b)) > limit {
		return nil, errors.New(subject + " registry response exceeds limit")
	}
	return b, nil
}
