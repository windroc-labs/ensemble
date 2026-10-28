package releases

import (
	"context"
	"io"
	"net/http"
	"strings"
	"testing"

	"ensemble/broker/internal/npm"
)

type roundTrip func(*http.Request) (*http.Response, error)

func (f roundTrip) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

// serve answers every request with status and body, recording each request's URL.
func serve(t *testing.T, status int, body string) (Registry, *[]string) {
	t.Helper()
	var urls []string
	r := New()
	r.http.Transport = roundTrip(func(req *http.Request) (*http.Response, error) {
		urls = append(urls, req.URL.String())
		return &http.Response{StatusCode: status, Body: io.NopCloser(strings.NewReader(body)), Header: make(http.Header), Request: req}, nil
	})
	return r, &urls
}

func TestLatestReadsTheLatestDistTag(t *testing.T) {
	r, urls := serve(t, http.StatusOK, `{"name":"@openai/codex","version":"0.164.0"}`)
	got, err := r.Latest(context.Background(), "@openai/codex")
	if err != nil || got != "0.164.0" {
		t.Fatalf("Latest = %q, %v", got, err)
	}
	if len(*urls) != 1 || (*urls)[0] != "https://registry.npmjs.org/@openai%2Fcodex/latest" {
		t.Fatalf("urls %q", *urls)
	}
}

func TestLatestRejects(t *testing.T) {
	for name, c := range map[string]struct {
		status int
		body   string
		want   string
	}{
		"another package": {http.StatusOK, `{"name":"@openai/codex-sdk","version":"0.164.0"}`, "invalid latest @openai/codex release"},
		"prerelease":      {http.StatusOK, `{"name":"@openai/codex","version":"0.164.0-alpha.1"}`, "invalid latest @openai/codex release"},
		"leading zero":    {http.StatusOK, `{"name":"@openai/codex","version":"0.0164.0"}`, "invalid latest @openai/codex release"},
		"not json":        {http.StatusOK, `<html>`, "invalid latest @openai/codex release"},
		"missing":         {http.StatusNotFound, `{}`, "@openai/codex registry release missing"},
		"oversize":        {http.StatusOK, `{"name":"@openai/codex","version":"0.164.0","x":"` + strings.Repeat("a", npm.MaxMetadata) + `"}`, "@openai/codex registry response exceeds limit"},
	} {
		t.Run(name, func(t *testing.T) {
			r, _ := serve(t, c.status, c.body)
			if got, err := r.Latest(context.Background(), "@openai/codex"); err == nil || err.Error() != c.want {
				t.Fatalf("Latest = %q, %v; want %q", got, err, c.want)
			}
		})
	}
}
