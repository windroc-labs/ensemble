// Package github is the broker's GitHub App client: the App JWT, its installations, installation tokens and the
// repositories each reaches, and its bot user's ID.
// Status errors carry the request method/path and a bounded, credential-redacted JSON message, never a full body.
package github

import (
	"bytes"
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
	"maps"
	"net/http"
	"net/url"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"
)

const (
	perPage   = 100
	maxPages  = 100
	maxBody   = 1 << 20
	apiTarget = "https://api.github.com"
)

// Login is a GitHub account login, safe as a file name and argv element.
var Login = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9-]{0,38}$`)

// fullName is a repository's owner/name.
var fullName = regexp.MustCompile(`^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$`)

// ValidRepo is whether s is a repository's owner/name that is safe in an API path: GitHub forbids the names . and ..
func ValidRepo(s string) bool {
	return fullName.MatchString(s) && !strings.HasSuffix(s, "/.") && !strings.HasSuffix(s, "/..")
}

// levels are the repository permissions a token may request, each with the levels GitHub accepts for it.
var levels = map[string][]string{
	"actions": {"read", "write"}, "administration": {"read", "write"}, "checks": {"read", "write"},
	"codespaces": {"read", "write"}, "contents": {"read", "write"}, "dependabot_secrets": {"read", "write"},
	"deployments": {"read", "write"}, "environments": {"read", "write"}, "issues": {"read", "write"},
	"metadata": {"read"}, "packages": {"read", "write"}, "pages": {"read", "write"},
	"pull_requests": {"read", "write"}, "repository_custom_properties": {"read", "write"},
	"repository_hooks": {"read", "write"}, "secret_scanning_alerts": {"read", "write"}, "secrets": {"read", "write"},
	"security_events": {"read", "write"}, "single_file": {"read", "write"}, "statuses": {"read", "write"},
	"vulnerability_alerts": {"read", "write"}, "workflows": {"write"},
}

// CheckPermissions is nil when permissions is a non-empty set of known repository permissions at levels GitHub
// accepts for each; else an error naming the first bad entry, by name.
func CheckPermissions(permissions map[string]string) error {
	if len(permissions) == 0 {
		return errors.New("no GitHub permissions")
	}
	for _, name := range slices.Sorted(maps.Keys(permissions)) {
		want, ok := levels[name]
		if !ok {
			return fmt.Errorf("unknown GitHub permission %q", name)
		}
		if level := permissions[name]; !slices.Contains(want, level) {
			return fmt.Errorf("GitHub permission %s: level %q, want %s", name, level, strings.Join(want, " or "))
		}
	}
	return nil
}

type Client struct {
	Base string
	HTTP *http.Client
	Now  func() time.Time
}

func New() Client {
	return Client{Base: apiTarget, HTTP: &http.Client{Timeout: 30 * time.Second}, Now: time.Now}
}

type Installation struct {
	ID      int64
	Account string
}

// StatusError is a response with an unexpected HTTP status.
type StatusError struct {
	Status                int
	method, path, message string
}

func (e *StatusError) Error() string {
	text := fmt.Sprintf("GitHub answered HTTP %d to %s %s", e.Status, e.method, e.path)
	if e.message != "" {
		text += ": " + strconv.Quote(e.message)
	}
	return text
}

var (
	githubToken = regexp.MustCompile(`gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+`)
	jwtToken    = regexp.MustCompile(`[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]*){2,}`)
)

// Redact removes every GitHub token, JWT and private key from text, and bearer ("": none) wherever it appears; callers
// redact before truncating so no partial token can escape at the boundary.
func Redact(text, bearer string) string {
	if strings.Contains(text, "PRIVATE KEY") {
		return "[redacted]"
	}
	text = githubToken.ReplaceAllString(text, "[redacted]")
	// Hide the whole dotted sequence, including adjacent text, rather than trusting a candidate header.
	text = jwtToken.ReplaceAllString(text, "[redacted]")
	if bearer != "" {
		text = strings.ReplaceAll(text, bearer, "[redacted]")
	}
	return text
}

type transportError struct{ op string }

func (e *transportError) Error() string { return "GitHub unreachable: " + e.op }

// Transport reports whether err got no HTTP response at all (offline, timeout).
func Transport(err error) bool {
	var te *transportError
	return errors.As(err, &te)
}

// ParseKey reads an App private key: the keychain item's base64 of the PEM file, or the PEM itself.
func ParseKey(data []byte) (*rsa.PrivateKey, error) {
	if decoded, err := base64.StdEncoding.DecodeString(string(bytes.TrimSpace(data))); err == nil {
		data = decoded
	}
	block, _ := pem.Decode(data)
	if block == nil {
		return nil, errors.New("App private key is not PEM")
	}
	if key, err := x509.ParsePKCS1PrivateKey(block.Bytes); err == nil {
		return key, nil
	}
	parsed, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	key, ok := parsed.(*rsa.PrivateKey)
	if err != nil || !ok {
		return nil, errors.New("App private key is not an RSA key")
	}
	return key, nil
}

// JWT is the App's 9-minute RS256 token, backdated a minute for clock drift.
func (c Client) JWT(appID string, key *rsa.PrivateKey) (string, error) {
	part := func(v any) string {
		data, _ := json.Marshal(v)
		return base64.RawURLEncoding.EncodeToString(data)
	}
	now := c.Now().Unix()
	signed := part(map[string]string{"alg": "RS256", "typ": "JWT"}) + "." + part(map[string]any{"iat": now - 60, "exp": now + 540, "iss": appID})
	sum := sha256.Sum256([]byte(signed))
	sig, err := rsa.SignPKCS1v15(rand.Reader, key, crypto.SHA256, sum[:])
	if err != nil {
		return "", err
	}
	return signed + "." + base64.RawURLEncoding.EncodeToString(sig), nil
}

// call sends one request authorized by bearer ("": none) and decodes a want-status JSON answer into out (nil: no body
// read).
func (c Client) call(ctx context.Context, method, path, bearer string, body any, want int, out any) error {
	var reader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return err
		}
		reader = bytes.NewReader(data)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.Base+path, reader)
	if err != nil {
		return err
	}
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("X-GitHub-Api-Version", "2022-11-28")
	req.Header.Set("User-Agent", "ensemble-broker")
	if bearer != "" {
		req.Header.Set("Authorization", "Bearer "+bearer)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return &transportError{method + " " + path}
	}
	defer resp.Body.Close()
	if resp.StatusCode != want {
		failure := &StatusError{Status: resp.StatusCode, method: method, path: Redact(req.URL.EscapedPath(), bearer)}
		var answer struct {
			Message string `json:"message"`
		}
		data, readErr := io.ReadAll(io.LimitReader(resp.Body, maxBody+1))
		if readErr == nil && len(data) <= maxBody && json.Unmarshal(data, &answer) == nil {
			message := []rune(Redact(answer.Message, bearer))
			failure.message = string(message[:min(len(message), 200)])
		}
		return failure
	}
	if out == nil {
		return nil
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, maxBody)).Decode(out); err != nil {
		return fmt.Errorf("GitHub answered unreadable JSON to %s %s", method, path)
	}
	return nil
}

// Installations lists every installation of the App, reading every page.
func (c Client) Installations(ctx context.Context, jwt string) ([]Installation, error) {
	var all []Installation
	for page := 1; ; page++ {
		var found []struct {
			ID      int64 `json:"id"`
			Account struct {
				Login string `json:"login"`
			} `json:"account"`
		}
		path := fmt.Sprintf("/app/installations?per_page=%d&page=%d", perPage, page)
		if err := c.call(ctx, http.MethodGet, path, jwt, nil, http.StatusOK, &found); err != nil {
			return nil, err
		}
		for _, f := range found {
			if f.ID <= 0 || !Login.MatchString(f.Account.Login) {
				return nil, errors.New("GitHub listed an installation without a valid id and account login")
			}
			all = append(all, Installation{f.ID, f.Account.Login})
		}
		if len(found) < perPage {
			return all, nil
		}
	}
}

// Issue creates an installation token for every repository of installation id, limited to permissions.
func (c Client) Issue(ctx context.Context, jwt string, id int64, permissions map[string]string) (string, time.Time, error) {
	return c.issue(ctx, jwt, id, map[string]any{"permissions": permissions})
}

// IssueRepo creates an installation token of installation id for repo (owner/name) alone, limited to permissions.
func (c Client) IssueRepo(ctx context.Context, jwt string, id int64, repo string, permissions map[string]string) (string, time.Time, error) {
	if !ValidRepo(repo) {
		return "", time.Time{}, fmt.Errorf("invalid repository %q", repo)
	}
	_, name, _ := strings.Cut(repo, "/")
	return c.issue(ctx, jwt, id, map[string]any{"permissions": permissions, "repositories": []string{name}})
}

func (c Client) issue(ctx context.Context, jwt string, id int64, body map[string]any) (string, time.Time, error) {
	var got struct {
		Token     string `json:"token"`
		ExpiresAt string `json:"expires_at"`
	}
	path := fmt.Sprintf("/app/installations/%d/access_tokens", id)
	if err := c.call(ctx, http.MethodPost, path, jwt, body, http.StatusCreated, &got); err != nil {
		return "", time.Time{}, err
	}
	expires, err := time.Parse(time.RFC3339, got.ExpiresAt)
	if got.Token == "" || err != nil {
		return "", time.Time{}, errors.New("GitHub answered a token without a token or expiry")
	}
	return got.Token, expires.UTC(), nil
}

// RepoInstallation is the App's installation on repo (owner/name); a StatusError 404 means it is not installed there.
func (c Client) RepoInstallation(ctx context.Context, jwt, repo string) (Installation, error) {
	if !ValidRepo(repo) {
		return Installation{}, fmt.Errorf("invalid repository %q", repo)
	}
	var got struct {
		ID      int64 `json:"id"`
		Account struct {
			Login string `json:"login"`
		} `json:"account"`
	}
	if err := c.call(ctx, http.MethodGet, "/repos/"+repo+"/installation", jwt, nil, http.StatusOK, &got); err != nil {
		return Installation{}, err
	}
	if got.ID <= 0 || !Login.MatchString(got.Account.Login) {
		return Installation{}, errors.New("GitHub answered an installation without a valid id and account login")
	}
	return Installation{got.ID, got.Account.Login}, nil
}

// DefaultBranch is repo's default branch, as token sees it.
func (c Client) DefaultBranch(ctx context.Context, token, repo string) (string, error) {
	if !ValidRepo(repo) {
		return "", fmt.Errorf("invalid repository %q", repo)
	}
	var got struct {
		DefaultBranch string `json:"default_branch"`
	}
	if err := c.call(ctx, http.MethodGet, "/repos/"+repo, token, nil, http.StatusOK, &got); err != nil {
		return "", err
	}
	if got.DefaultBranch == "" {
		return "", errors.New("GitHub answered no default branch")
	}
	return got.DefaultBranch, nil
}

// RefKind is "branch" when repo has branch ref, else "tag" when it has tag ref, else "". ref must be a branch or tag
// name the caller checked: it goes into the request path as is.
func (c Client) RefKind(ctx context.Context, token, repo, ref string) (string, error) {
	if !ValidRepo(repo) {
		return "", fmt.Errorf("invalid repository %q", repo)
	}
	for _, kind := range []struct{ prefix, name string }{{"heads/", "branch"}, {"tags/", "tag"}} {
		var got struct {
			Ref string `json:"ref"`
		}
		err := c.call(ctx, http.MethodGet, "/repos/"+repo+"/git/ref/"+kind.prefix+ref, token, nil, http.StatusOK, &got)
		var se *StatusError
		switch {
		case errors.As(err, &se) && se.Status == http.StatusNotFound:
		case err != nil:
			return "", err
		case got.Ref == "refs/"+kind.prefix+ref:
			return kind.name, nil
		}
	}
	return "", nil
}

// Repositories is what installation token reaches: its installation's repository_selection and every repository's
// full name, sorted, reading every page up to maxPages.
func (c Client) Repositories(ctx context.Context, token string) (string, []string, error) {
	names := []string{}
	var selection string
	for page := 1; page <= maxPages; page++ {
		var got struct {
			Total        int    `json:"total_count"`
			Selection    string `json:"repository_selection"`
			Repositories []struct {
				FullName string `json:"full_name"`
			} `json:"repositories"`
		}
		path := fmt.Sprintf("/installation/repositories?per_page=%d&page=%d", perPage, page)
		if err := c.call(ctx, http.MethodGet, path, token, nil, http.StatusOK, &got); err != nil {
			return "", nil, err
		}
		if got.Selection != "all" && got.Selection != "selected" || selection != "" && got.Selection != selection {
			return "", nil, errors.New("GitHub answered no valid repository_selection")
		}
		selection = got.Selection
		for _, r := range got.Repositories {
			if !fullName.MatchString(r.FullName) {
				return "", nil, errors.New("GitHub listed a repository without a valid full name")
			}
			names = append(names, r.FullName)
		}
		if len(got.Repositories) < perPage || len(names) >= got.Total {
			slices.Sort(names)
			return selection, names, nil
		}
	}
	return "", nil, fmt.Errorf("GitHub listed more than %d repositories", maxPages*perPage)
}

// ErrAlreadyInvalid is Revoke's answer for a token GitHub no longer accepts: as good as revoked.
var ErrAlreadyInvalid = errors.New("GitHub token already invalid")

// Revoke invalidates token; 401 and 404 mean it is already invalid (ErrAlreadyInvalid).
func (c Client) Revoke(ctx context.Context, token string) error {
	err := c.call(ctx, http.MethodDelete, "/installation/token", token, nil, http.StatusNoContent, nil)
	var se *StatusError
	if errors.As(err, &se) && (se.Status == http.StatusUnauthorized || se.Status == http.StatusNotFound) {
		return ErrAlreadyInvalid
	}
	return err
}

// AppSlug is the slug of the App jwt signs for.
func (c Client) AppSlug(ctx context.Context, jwt string) (string, error) {
	var got struct {
		Slug string `json:"slug"`
	}
	if err := c.call(ctx, http.MethodGet, "/app", jwt, nil, http.StatusOK, &got); err != nil {
		return "", err
	}
	if !Login.MatchString(got.Slug) {
		return "", errors.New("GitHub answered no valid App slug")
	}
	return got.Slug, nil
}

// BotUser is the user ID of bot login (`<app>[bot]`), from the public user API, sent without a credential.
func (c Client) BotUser(ctx context.Context, login string) (int64, error) {
	var got struct {
		Login string `json:"login"`
		ID    int64  `json:"id"`
		Type  string `json:"type"`
	}
	if err := c.call(ctx, http.MethodGet, "/users/"+url.PathEscape(login), "", nil, http.StatusOK, &got); err != nil {
		return 0, err
	}
	if got.Login != login || got.Type != "Bot" || got.ID < 1 {
		return 0, errors.New("GitHub answered no bot user with that login and a valid id")
	}
	return got.ID, nil
}
