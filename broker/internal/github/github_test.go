package github

import (
	"context"
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"
)

var now = time.Date(2026, 10, 17, 12, 0, 0, 0, time.UTC)

func testKey(t *testing.T) *rsa.PrivateKey {
	t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	return key
}

func client(t *testing.T, h http.HandlerFunc) Client {
	t.Helper()
	srv := httptest.NewServer(h)
	t.Cleanup(srv.Close)
	return Client{Base: srv.URL, HTTP: srv.Client(), Now: func() time.Time { return now }}
}

func TestParseKey(t *testing.T) {
	key := testKey(t)
	pkcs1 := pem.EncodeToMemory(&pem.Block{Type: "RSA PRIVATE KEY", Bytes: x509.MarshalPKCS1PrivateKey(key)})
	der, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		t.Fatal(err)
	}
	pkcs8 := pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der})
	for name, data := range map[string][]byte{
		"pkcs1":        pkcs1,
		"pkcs8":        pkcs8,
		"base64 pkcs1": []byte(base64.StdEncoding.EncodeToString(pkcs1)),
		"base64 crlf":  []byte(base64.StdEncoding.EncodeToString(pkcs8) + "\r\n"),
	} {
		got, err := ParseKey(data)
		if err != nil || !got.Equal(key) {
			t.Errorf("%s: %v", name, err)
		}
	}
	for name, data := range map[string][]byte{
		"empty":   nil,
		"garbage": []byte("not a key"),
		"cert":    pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: []byte("x")}),
	} {
		if _, err := ParseKey(data); err == nil {
			t.Errorf("%s: parsed", name)
		}
	}
}

func TestJWT(t *testing.T) {
	key := testKey(t)
	jwt, err := Client{Now: func() time.Time { return now }}.JWT("1234567", key)
	if err != nil {
		t.Fatal(err)
	}
	parts := strings.Split(jwt, ".")
	if len(parts) != 3 {
		t.Fatalf("parts %d", len(parts))
	}
	decode := func(s string) []byte {
		b, err := base64.RawURLEncoding.DecodeString(s)
		if err != nil {
			t.Fatal(err)
		}
		return b
	}
	var header, claims map[string]any
	if json.Unmarshal(decode(parts[0]), &header) != nil || json.Unmarshal(decode(parts[1]), &claims) != nil {
		t.Fatal("bad json")
	}
	if header["alg"] != "RS256" || header["typ"] != "JWT" {
		t.Errorf("header %v", header)
	}
	if claims["iss"] != "1234567" || claims["iat"] != float64(now.Unix()-60) || claims["exp"] != float64(now.Unix()+540) {
		t.Errorf("claims %v", claims)
	}
	sum := sha256.Sum256([]byte(parts[0] + "." + parts[1]))
	if err := rsa.VerifyPKCS1v15(&key.PublicKey, crypto.SHA256, sum[:], decode(parts[2])); err != nil {
		t.Errorf("signature: %v", err)
	}
}

func TestInstallationsPaginates(t *testing.T) {
	var pages []string
	c := client(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/app/installations" || r.Header.Get("Authorization") != "Bearer J" ||
			r.Header.Get("Accept") != "application/vnd.github+json" || r.Header.Get("X-GitHub-Api-Version") != "2022-11-28" {
			t.Errorf("request %s %s %v", r.Method, r.URL, r.Header)
		}
		pages = append(pages, r.URL.Query().Get("page")+"/"+r.URL.Query().Get("per_page"))
		n := 100
		if r.URL.Query().Get("page") == "2" {
			n = 1
		}
		var items []string
		for i := range n {
			items = append(items, fmt.Sprintf(`{"id":%d,"account":{"login":"owner-%d"}}`, i+1, i))
		}
		_, _ = io.WriteString(w, "["+strings.Join(items, ",")+"]")
	})
	got, err := c.Installations(context.Background(), "J")
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 101 || got[0] != (Installation{ID: 1, Account: "owner-0"}) || strings.Join(pages, " ") != "1/100 2/100" {
		t.Fatalf("got %d %v pages %v", len(got), got[:1], pages)
	}
}

func TestInstallationsRejectsBadLogin(t *testing.T) {
	for _, login := range []string{"../x", ".hidden", "", "-dash", strings.Repeat("a", 40), "a b"} {
		c := client(t, func(w http.ResponseWriter, _ *http.Request) {
			_, _ = fmt.Fprintf(w, `[{"id":1,"account":{"login":%q}}]`, login)
		})
		if _, err := c.Installations(context.Background(), "J"); err == nil {
			t.Errorf("%q accepted", login)
		}
	}
}

func TestIssueBody(t *testing.T) {
	c := client(t, func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		if r.Method != http.MethodPost || r.URL.Path != "/app/installations/42/access_tokens" || r.Header.Get("Authorization") != "Bearer J" ||
			string(body) != `{"permissions":{"contents":"write","statuses":"read","workflows":"write"}}` {
			t.Errorf("request %s %s %q", r.Method, r.URL, body)
		}
		w.WriteHeader(http.StatusCreated)
		_, _ = io.WriteString(w, `{"token":"ghs_secret","expires_at":"2026-10-17T13:00:00Z","permissions":{}}`)
	})
	token, expires, err := c.Issue(context.Background(), "J", 42, map[string]string{"contents": "write", "statuses": "read", "workflows": "write"})
	if err != nil || token != "ghs_secret" || !expires.Equal(now.Add(time.Hour)) {
		t.Fatalf("%q %v %v", token, expires, err)
	}
}

// defaultSet is the packaged config's set, as session sends it.
var defaultSet = map[string]string{"contents": "write", "pull_requests": "write", "workflows": "write", "checks": "read", "actions": "read", "statuses": "read"}

func TestCheckPermissions(t *testing.T) {
	if err := CheckPermissions(defaultSet); err != nil {
		t.Fatal(err)
	}
	if err := CheckPermissions(map[string]string{"metadata": "read", "issues": "write"}); err != nil {
		t.Fatal(err)
	}
	for want, set := range map[string]map[string]string{
		"no GitHub permissions":                                         {},
		`unknown GitHub permission "foo"`:                               {"contents": "write", "foo": "read"},
		`unknown GitHub permission "Contents"`:                          {"Contents": "write"},
		`GitHub permission contents: level "admin", want read or write`: {"contents": "admin"},
		`GitHub permission metadata: level "write", want read`:          {"metadata": "write"},
		`GitHub permission workflows: level "read", want write`:         {"workflows": "read"},
		`GitHub permission statuses: level "", want read or write`:      {"statuses": ""},
	} {
		if err := CheckPermissions(set); err == nil || err.Error() != want {
			t.Errorf("%v: got %v, want %q", set, err, want)
		}
	}
}

func TestIssueFailures(t *testing.T) {
	for name, h := range map[string]http.HandlerFunc{
		"status": func(w http.ResponseWriter, _ *http.Request) { http.Error(w, "ghs_leak", http.StatusForbidden) },
		"no token": func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(201)
			_, _ = io.WriteString(w, `{"expires_at":"2026-10-17T13:00:00Z"}`)
		},
		"bad expiry": func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(201)
			_, _ = io.WriteString(w, `{"token":"ghs_leak","expires_at":"soon"}`)
		},
	} {
		_, _, err := client(t, h).Issue(context.Background(), "J", 1, defaultSet)
		if err == nil || strings.Contains(err.Error(), "ghs_leak") {
			t.Errorf("%s: %v", name, err)
		}
	}
	var se *StatusError
	_, _, err := client(t, func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusForbidden) }).Issue(context.Background(), "J", 1, defaultSet)
	if !errors.As(err, &se) || se.Status != http.StatusForbidden || Transport(err) {
		t.Errorf("status error %v", err)
	}
}

func TestStatusErrorResponses(t *testing.T) {
	const prefix = "GitHub answered HTTP 422 to POST /app/installations/42/access_tokens"
	for _, tc := range []struct {
		name, body, message string
	}{
		{"message", `{"message":"Validation Failed","errors":[{"message":"ignored"}],"token":"fake-other-field"}`, "Validation Failed"},
		{"html", `<html>fake-body-must-not-escape</html>`, ""},
		{"malformed", `{"message":"not complete"`, ""},
		{"trailing garbage", `{"message":"not valid"}garbage`, ""},
		{"missing", `{"documentation_url":"https://docs.github.com"}`, ""},
		{"null", `{"message":null}`, ""},
		{"wrong type", `{"message":{"detail":"ignored"}}`, ""},
		{"empty", `{"message":""}`, ""},
		{"boundary", `{"message":"` + strings.Repeat("a", 200) + `"}`, strings.Repeat("a", 200)},
		{"long unicode", `{"message":"` + strings.Repeat("错", 201) + `"}`, strings.Repeat("错", 200)},
		{"oversized body", `{"message":"small","ignored":"` + strings.Repeat("a", maxBody) + `"}`, ""},
		{"control characters", `{"message":"bad\nrequest\u001b[31m"}`, "bad\nrequest\x1b[31m"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			c := client(t, func(w http.ResponseWriter, _ *http.Request) {
				w.WriteHeader(http.StatusUnprocessableEntity)
				_, _ = io.WriteString(w, tc.body)
			})
			_, _, err := c.Issue(context.Background(), "fake-request-credential", 42, defaultSet)
			var se *StatusError
			if !errors.As(err, &se) || se.Status != 422 || Transport(err) {
				t.Fatal("expected HTTP status error")
			}
			want := prefix
			if tc.message != "" {
				want += ": " + strconv.Quote(tc.message)
			}
			if err.Error() != want {
				t.Errorf("got %q, want %q", err.Error(), want)
			}
		})
	}
}

func TestStatusErrorRedactsCredentials(t *testing.T) {
	bearer := "fake-opaque-credential"
	jwt := base64.RawURLEncoding.EncodeToString([]byte(` {"alg":"RS256"}`)) + "." +
		base64.RawURLEncoding.EncodeToString([]byte(`{"iss":"fake-app"}`)) + ".fake-signature"
	for name, message := range map[string]string{
		"bearer":       "Rejected " + bearer,
		"github token": "Rejected ghs_fakeResponseCredential",
		"PAT":          "Rejected github_pat_fakeResponseCredential",
		"JWT":          "Rejected " + jwt,
		"JWT adjacent": "prefix" + jwt,
		"JWT dotted":   "prefix." + jwt,
		"JWT suffix":   jwt + ".suffix",
		"unsigned JWT": strings.TrimSuffix(jwt, "fake-signature"),
		"private key":  "-----BEGIN RSA PRIVATE KEY-----\nfake-key-material\n-----END RSA PRIVATE KEY-----",
		"cut token":    strings.Repeat("a", 195) + "ghs_fakeResponseCredential",
	} {
		t.Run(name, func(t *testing.T) {
			c := client(t, func(w http.ResponseWriter, _ *http.Request) {
				w.WriteHeader(http.StatusUnprocessableEntity)
				_ = json.NewEncoder(w).Encode(map[string]string{"message": message})
			})
			err := c.call(context.Background(), http.MethodGet, "/installation/token?access_token="+bearer, bearer, nil, http.StatusOK, nil)
			if err == nil {
				t.Fatal("expected error")
			}
			secrets := append(strings.Split(jwt, "."), bearer, "ghs_", "github_pat_", "fake-key-material", "PRIVATE KEY", "access_token")
			for _, secret := range secrets {
				if strings.Contains(err.Error(), secret) {
					t.Fatal("credential or query leaked in error")
				}
			}
			if !strings.Contains(err.Error(), "GET /installation/token") || !strings.Contains(err.Error(), "[reda") {
				t.Fatal("missing request context or redaction")
			}
		})
	}
}

func TestRevoke(t *testing.T) {
	for status, ok := range map[int]bool{204: true, 401: true, 404: true, 500: false, 403: false} {
		c := client(t, func(w http.ResponseWriter, r *http.Request) {
			if r.Method != http.MethodDelete || r.URL.Path != "/installation/token" || r.Header.Get("Authorization") != "Bearer ghs_old" {
				t.Errorf("request %s %s", r.Method, r.URL)
			}
			w.WriteHeader(status)
		})
		if err := c.Revoke(context.Background(), "ghs_old"); (err == nil) != ok {
			t.Errorf("%d: %v", status, err)
		}
	}
}

func TestTransport(t *testing.T) {
	srv := httptest.NewServer(http.NotFoundHandler())
	srv.Close()
	c := Client{Base: srv.URL, HTTP: srv.Client(), Now: time.Now}
	err := c.Revoke(context.Background(), "ghs_x")
	if err == nil || !Transport(err) || strings.Contains(err.Error(), "ghs_x") {
		t.Fatalf("%v", err)
	}
	if Transport(&StatusError{Status: 500}) || Transport(nil) {
		t.Fatal("status counted as transport")
	}
	if New().Base != "https://api.github.com" || New().HTTP.Timeout != 30*time.Second {
		t.Fatal(strconv.Quote(New().Base))
	}
}

func TestBotUser(t *testing.T) {
	c := client(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.EscapedPath() != "/users/ensemble-bot%5Bbot%5D" || r.Header.Get("Authorization") != "" ||
			r.Header.Get("Accept") != "application/vnd.github+json" {
			t.Errorf("request %s %s %v", r.Method, r.URL.EscapedPath(), r.Header)
		}
		_, _ = io.WriteString(w, `{"login":"ensemble-bot[bot]","id":343764394,"type":"Bot","node_id":"x"}`)
	})
	id, err := c.BotUser(context.Background(), "ensemble-bot[bot]")
	if err != nil || id != 343764394 {
		t.Fatalf("%d %v", id, err)
	}
}

func TestAppSlug(t *testing.T) {
	c := client(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/app" || r.Header.Get("Authorization") != "Bearer jwt" {
			t.Errorf("request %s %s %v", r.Method, r.URL.Path, r.Header)
		}
		_, _ = io.WriteString(w, `{"id":5405554,"slug":"ensemble-test-bot","name":"ensemble test bot"}`)
	})
	if slug, err := c.AppSlug(context.Background(), "jwt"); err != nil || slug != "ensemble-test-bot" {
		t.Fatalf("%q %v", slug, err)
	}
	for _, body := range []string{`{"slug":""}`, `{"slug":"a b"}`, `{}`, `<html>`} {
		c := client(t, func(w http.ResponseWriter, _ *http.Request) { _, _ = io.WriteString(w, body) })
		if _, err := c.AppSlug(context.Background(), "jwt"); err == nil {
			t.Errorf("%s: no error", body)
		}
	}
}

func TestBotUserRejects(t *testing.T) {
	for name, body := range map[string]string{
		"wrong login": `{"login":"someone[bot]","id":1,"type":"Bot"}`,
		"user type":   `{"login":"ensemble-bot[bot]","id":1,"type":"User"}`,
		"zero id":     `{"login":"ensemble-bot[bot]","id":0,"type":"Bot"}`,
		"no id":       `{"login":"ensemble-bot[bot]","type":"Bot"}`,
		"not json":    `<html>`,
	} {
		c := client(t, func(w http.ResponseWriter, _ *http.Request) { _, _ = io.WriteString(w, body) })
		if id, err := c.BotUser(context.Background(), "ensemble-bot[bot]"); err == nil {
			t.Errorf("%s: accepted %d", name, id)
		}
	}
	var se *StatusError
	_, err := client(t, func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusNotFound) }).BotUser(context.Background(), "ensemble-bot[bot]")
	if !errors.As(err, &se) || se.Status != http.StatusNotFound {
		t.Errorf("status error %v", err)
	}
}

// repos serves pages of n repositories in all, per_page each, under selection.
func repos(t *testing.T, n int, selection string, seen *[]string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/installation/repositories" || r.Header.Get("Authorization") != "Bearer ghs_inst" {
			t.Errorf("%s %s %q", r.Method, r.URL.Path, r.Header.Get("Authorization"))
		}
		*seen = append(*seen, r.URL.RawQuery)
		page, _ := strconv.Atoi(r.URL.Query().Get("page"))
		var list []map[string]string
		for i := (page - 1) * perPage; i < n && i < page*perPage; i++ {
			list = append(list, map[string]string{"full_name": fmt.Sprintf("acme/r%03d", n-i)})
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"total_count": n, "repository_selection": selection, "repositories": list})
	}
}

func TestRepositoriesPaginates(t *testing.T) {
	var seen []string
	selection, names, err := client(t, repos(t, 150, "selected", &seen)).Repositories(context.Background(), "ghs_inst")
	if err != nil || selection != "selected" || len(names) != 150 || names[0] != "acme/r001" || names[149] != "acme/r150" {
		t.Fatalf("%v %q %d %v", err, selection, len(names), names[:2])
	}
	if strings.Join(seen, " ") != "per_page=100&page=1 per_page=100&page=2" {
		t.Fatal(seen)
	}

	seen = nil
	if _, names, err := client(t, repos(t, 100, "all", &seen)).Repositories(context.Background(), "ghs_inst"); err != nil || len(names) != 100 || len(seen) != 1 {
		t.Fatalf("a full last page stops at total_count: %v %d %v", err, len(names), seen)
	}
	seen = nil
	if _, names, err := client(t, repos(t, 0, "selected", &seen)).Repositories(context.Background(), "ghs_inst"); err != nil || names == nil || len(names) != 0 {
		t.Fatalf("no repositories is an empty list: %v %v", err, names)
	}
}

func TestRepositoriesRejects(t *testing.T) {
	answer := func(body string) http.HandlerFunc {
		return func(w http.ResponseWriter, _ *http.Request) { _, _ = io.WriteString(w, body) }
	}
	for name, h := range map[string]http.HandlerFunc{
		"selection": answer(`{"total_count":1,"repository_selection":"some","repositories":[{"full_name":"acme/a"}]}`),
		"name":      answer(`{"total_count":1,"repository_selection":"all","repositories":[{"full_name":"acme/a b"}]}`),
		"no owner":  answer(`{"total_count":1,"repository_selection":"all","repositories":[{"full_name":"a"}]}`),
		"json":      answer(`not json`),
		"status":    func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusInternalServerError) },
		"endless": func(w http.ResponseWriter, r *http.Request) {
			list := make([]map[string]string, perPage)
			for i := range list {
				list[i] = map[string]string{"full_name": "acme/x" + r.URL.Query().Get("page") + "-" + strconv.Itoa(i)}
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"total_count": 1 << 30, "repository_selection": "all", "repositories": list})
		},
	} {
		_, names, err := client(t, h).Repositories(context.Background(), "ghs_inst")
		if err == nil || names != nil {
			t.Errorf("%s: %v %d", name, err, len(names))
		}
		if err != nil && strings.Contains(err.Error(), "ghs_inst") {
			t.Errorf("%s: error carries the token", name)
		}
	}
	var se *StatusError
	if _, _, err := client(t, func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusForbidden) }).Repositories(context.Background(), "ghs_inst"); !errors.As(err, &se) || se.Status != 403 {
		t.Fatal(err)
	}
	c := Client{Base: "http://127.0.0.1:1", HTTP: &http.Client{Timeout: time.Second}, Now: time.Now}
	if _, _, err := c.Repositories(context.Background(), "ghs_inst"); !Transport(err) {
		t.Fatal(err)
	}
}
