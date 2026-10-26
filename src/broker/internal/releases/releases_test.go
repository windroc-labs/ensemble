package releases

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// serve answers every request with status and body, recording the request path.
func serve(t *testing.T, status int, body string) (Registry, *string) {
	t.Helper()
	var path string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		path = r.URL.EscapedPath()
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(server.Close)
	r := New()
	r.base = server.URL
	return r, &path
}

func TestLatestReadsTheLatestDistTag(t *testing.T) {
	r, path := serve(t, http.StatusOK, `{"name":"@openai/codex","version":"0.164.0"}`)
	got, err := r.Latest(context.Background(), "@openai/codex")
	if err != nil || got != "0.164.0" {
		t.Fatalf("Latest = %q, %v", got, err)
	}
	if *path != "/@openai%2Fcodex/latest" {
		t.Fatalf("path %q", *path)
	}
}

func TestLatestRejects(t *testing.T) {
	for name, c := range map[string]struct {
		status int
		body   string
	}{
		"another package": {http.StatusOK, `{"name":"@openai/codex-sdk","version":"0.164.0"}`},
		"prerelease":      {http.StatusOK, `{"name":"@openai/codex","version":"0.164.0-alpha.1"}`},
		"leading zero":    {http.StatusOK, `{"name":"@openai/codex","version":"0.0164.0"}`},
		"not json":        {http.StatusOK, `<html>`},
		"missing":         {http.StatusNotFound, `{}`},
		"server error":    {http.StatusInternalServerError, `{}`},
		"oversize":        {http.StatusOK, `{"name":"@openai/codex","version":"0.164.0","x":"` + strings.Repeat("a", maxMetadata) + `"}`},
	} {
		t.Run(name, func(t *testing.T) {
			r, _ := serve(t, c.status, c.body)
			if got, err := r.Latest(context.Background(), "@openai/codex"); err == nil {
				t.Fatalf("Latest = %q, want an error", got)
			}
		})
	}
}

func TestLatestFollowsNoRedirect(t *testing.T) {
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"name":"opencode-ai","version":"9.9.9"}`))
	}))
	defer target.Close()
	r, _ := serve(t, http.StatusOK, "")
	redirect := httptest.NewServer(http.RedirectHandler(target.URL+"/opencode-ai/latest", http.StatusFound))
	defer redirect.Close()
	r.base = redirect.URL
	if got, err := r.Latest(context.Background(), "opencode-ai"); err == nil {
		t.Fatalf("Latest = %q through a redirect", got)
	}
}

func TestLatestErrorsCarryNoBody(t *testing.T) {
	r, _ := serve(t, http.StatusOK, `{"name":"opencode-ai","version":"secret-ish"}`)
	_, err := r.Latest(context.Background(), "opencode-ai")
	if err == nil || strings.Contains(err.Error(), "secret-ish") {
		t.Fatalf("error %v", err)
	}
}
