package npm

import (
	"context"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"
)

type roundTrip func(*http.Request) (*http.Response, error)

func (f roundTrip) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

// unread fails the test when Fetch reads a body it should have rejected first.
type unread struct{ t *testing.T }

func (u unread) Read([]byte) (int, error) { u.t.Fatal("body read"); return 0, io.EOF }

func reply(status int, body string) roundTrip {
	return func(r *http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: status, Body: io.NopCloser(strings.NewReader(body)), Header: make(http.Header), Request: r}, nil
	}
}

func fetch(t *testing.T, rt roundTrip, raw string, limit int64) ([]byte, error) {
	t.Helper()
	return Fetch(context.Background(), &http.Client{Transport: rt}, "Thing", raw, limit)
}

func TestFetchReturnsTheBodyAndSendsTheUserAgent(t *testing.T) {
	var agent, url string
	b, err := fetch(t, func(r *http.Request) (*http.Response, error) {
		agent, url = r.Header.Get("User-Agent"), r.URL.String()
		return reply(http.StatusOK, "body")(r)
	}, Package("@openai/codex", "latest"), 4)
	if err != nil || string(b) != "body" {
		t.Fatalf("Fetch = %q, %v", b, err)
	}
	if agent != "ensemble-broker" || url != "https://registry.npmjs.org/@openai%2Fcodex/latest" {
		t.Fatalf("agent %q url %q", agent, url)
	}
}

func TestFetchErrors(t *testing.T) {
	for name, c := range map[string]struct {
		rt    roundTrip
		raw   string
		limit int64
		want  string
	}{
		"another origin": {reply(http.StatusOK, "x"), "https://evil.example/x", 4, "Thing download origin rejected"},
		"transport":      {func(*http.Request) (*http.Response, error) { return nil, errors.New("dial: secret") }, registry + "/x", 4, "Thing registry unavailable"},
		"missing":        {reply(http.StatusNotFound, "secret"), registry + "/x", 4, "Thing registry release missing"},
		"server error":   {reply(http.StatusInternalServerError, "secret"), registry + "/x", 4, "Thing registry unavailable"},
		"over the limit": {reply(http.StatusOK, "secret"), registry + "/x", 4, "Thing registry response exceeds limit"},
	} {
		t.Run(name, func(t *testing.T) {
			b, err := fetch(t, c.rt, c.raw, c.limit)
			if err == nil || err.Error() != c.want {
				t.Fatalf("Fetch = %q, %v; want %q", b, err, c.want)
			}
		})
	}
}

func TestFetchFollowsNoRedirect(t *testing.T) {
	requests := 0
	_, err := fetch(t, func(r *http.Request) (*http.Response, error) {
		requests++
		return &http.Response{StatusCode: http.StatusFound, Header: http.Header{"Location": []string{registry + "/y"}}, Body: io.NopCloser(strings.NewReader("")), Request: r}, nil
	}, registry+"/x", 4)
	if err == nil || err.Error() != "Thing registry unavailable" || requests != 1 {
		t.Fatalf("error %v after %d requests", err, requests)
	}
}

func TestFetchRejectsAnOversizeContentLengthUnread(t *testing.T) {
	_, err := fetch(t, func(r *http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: http.StatusOK, ContentLength: 5, Body: io.NopCloser(unread{t}), Header: make(http.Header), Request: r}, nil
	}, registry+"/x", 4)
	if err == nil || err.Error() != "Thing registry unavailable" {
		t.Fatalf("error %v", err)
	}
}

func TestAllowed(t *testing.T) {
	for raw, want := range map[string]bool{
		"https://registry.npmjs.org/x.tgz":      true,
		"http://registry.npmjs.org/x.tgz":       false,
		"https://registry.npmjs.org.evil/x":     false,
		"https://user@registry.npmjs.org/x":     false,
		"https://registry.npmjs.org/x?y=1":      false,
		"https://registry.npmjs.org/x#y":        false,
		"https:registry.npmjs.org/x":            false,
		"https://registry.npmjs.org":            false,
		"https://registry.npmjs.org:8443/x.tgz": false,
		"%":                                     false,
	} {
		if got := Allowed(raw); got != want {
			t.Errorf("Allowed(%q) = %v", raw, got)
		}
	}
}

func TestPackageEscapesTheScope(t *testing.T) {
	if got := Package("opencode-ai", "1.2.3"); got != "https://registry.npmjs.org/opencode-ai/1.2.3" {
		t.Fatal(got)
	}
	if got := Package("@anthropic-ai/claude-code", "latest"); got != "https://registry.npmjs.org/@anthropic-ai%2Fclaude-code/latest" {
		t.Fatal(got)
	}
}
