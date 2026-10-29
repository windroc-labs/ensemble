package broker

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"maps"
	"net/http"
	"net/http/httptest"
	"slices"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"ensemble/broker/internal/credential"
	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/github"
	"ensemble/broker/internal/protocol"
)

const ctr = "ensemble-worker_1-0123456789abcdef0123456789abcdef"

// writeScript is the token file's atomic replacement, as every broker before the shared delivery wrote it.
const writeScript = "mkdir -p -m 0700 " + tokenDir + " && umask 0377 && rm -f " + tokenDir + `/."$1".new && cat > ` + tokenDir +
	`/."$1".new && mv -f ` + tokenDir + `/."$1".new ` + tokenDir + `/"$1"`

var (
	clock    = time.Date(2026, 10, 17, 12, 0, 0, 0, time.UTC)
	keyOnce  sync.Once
	appKeyB  []byte
	errLeaky = errors.New("keychain status -25293")
)

func testAppKey(t *testing.T) []byte {
	t.Helper()
	keyOnce.Do(func() {
		key, err := rsa.GenerateKey(rand.Reader, 2048)
		if err != nil {
			t.Fatal(err)
		}
		pemKey := pem.EncodeToMemory(&pem.Block{Type: "RSA PRIVATE KEY", Bytes: x509.MarshalPKCS1PrivateKey(key)})
		appKeyB = []byte(base64.StdEncoding.EncodeToString(pemKey))
	})
	return appKeyB
}

// secrets is a fake broker-only keychain recording every call as "<op> <service>"; unreadable makes a service's
// reads fail as a reinstalled broker's would.
type secrets struct {
	items      map[string][]byte
	unreadable map[string]bool
	putErr     error
	calls      []string
}

func (s *secrets) Secret(service, account string) ([]byte, error) {
	s.calls = append(s.calls, "read "+service)
	if s.unreadable[service] {
		return nil, errLeaky
	}
	b, ok := s.items[service+"/"+account]
	if !ok {
		return nil, ErrNotFound
	}
	return slices.Clone(b), nil
}

func (s *secrets) PutSecret(service, account string, data []byte) error {
	s.calls = append(s.calls, "put "+service)
	if s.putErr != nil {
		return s.putErr
	}
	s.items[service+"/"+account] = slices.Clone(data)
	return nil
}

func (s *secrets) DeleteSecret(service, account string) error {
	s.calls = append(s.calls, "delete "+service)
	delete(s.items, service+"/"+account)
	return nil
}

type keys struct {
	account string
	key     []byte
	err     error
}

func (k keys) AppAccount(app string) (string, error) {
	if app != "ensemble-broker-bot" || k.err != nil {
		return "", errors.Join(ErrNotFound, k.err)
	}
	return k.account, nil
}

func (k keys) AppKey(app, account string) ([]byte, error) {
	if account != k.account {
		return nil, ErrNotFound
	}
	return slices.Clone(k.key), nil
}

// api is a fake GitHub: installations, issued tokens and revocations.
type api struct {
	mu        sync.Mutex
	installs  []string
	issued    int
	revoked   []string
	issueFail map[int]int // nth issue (1-based) → status
	onIssue   func(n int) // before the nth issue's answer
	tokenOf   func(id string, n int) string
	revokeErr func(token string) int
	revokeMsg string // a failed revocation's message; %s is the token
	slug      string // GET /app's slug; "": 404
	appAuth   []string
	user      string // the bot user answer; "": 404
	userPath  string
	userAuth  []string
	reposFail int      // the repository listing's status; 0: each installation reaches <login>/one and <login>/two
	bodies    []string // each token request's body
	repo      *fakeRepo
	repoPaths []string            // each /repos/ request's path and its token
	scoped    map[string][]string // each repository-limited token's repository names
	installOf map[string]int      // each token's installation ID
}

// fakeRepo is the one repository a prefetch test reaches: acme/app, on the acme installation when installed.
type fakeRepo struct {
	installed      bool
	defaultBranch  string
	branches, tags []string
	refStatus      int // a ref lookup's status; 0: 200 or 404 by branches and tags
}

// perms is the set every test token is requested with; permsBody, its request body.
var perms = map[string]string{"contents": "write", "statuses": "read", "workflows": "write"}

const permsBody = `{"permissions":{"contents":"write","statuses":"read","workflows":"write"}}`

// tok is the fake's nth token, for installation id.
func tok(id, n int) string { return fmt.Sprintf("ghs_%dT%d", id, n) }

func (a *api) handler(t *testing.T) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		a.mu.Lock()
		defer a.mu.Unlock()
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/app/installations":
			var items []string
			for i, login := range a.installs {
				if login != "" {
					items = append(items, fmt.Sprintf(`{"id":%d,"account":{"login":%q}}`, i+1, login))
				}
			}
			_, _ = w.Write([]byte("[" + strings.Join(items, ",") + "]"))
		case r.Method == http.MethodPost && strings.HasPrefix(r.URL.Path, "/app/installations/"):
			body, _ := io.ReadAll(r.Body)
			a.bodies = append(a.bodies, string(body))
			a.issued++
			if a.onIssue != nil {
				a.onIssue(a.issued)
			}
			if status := a.issueFail[a.issued]; status != 0 {
				w.WriteHeader(status)
				return
			}
			id := strings.TrimSuffix(strings.TrimPrefix(r.URL.Path, "/app/installations/"), "/access_tokens")
			installation, err := strconv.Atoi(id)
			if err != nil {
				t.Errorf("issue for %q", id)
			}
			token := tok(installation, a.issued)
			if a.tokenOf != nil {
				token = a.tokenOf(id, a.issued)
			}
			if a.installOf == nil {
				a.installOf = map[string]int{}
			}
			a.installOf[token] = installation
			var req struct{ Repositories []string }
			if json.Unmarshal(body, &req) == nil && req.Repositories != nil {
				if a.scoped == nil {
					a.scoped = map[string][]string{}
				}
				a.scoped[token] = req.Repositories
			}
			w.WriteHeader(http.StatusCreated)
			_, _ = fmt.Fprintf(w, `{"token":%q,"expires_at":"2026-10-17T13:00:00Z"}`, token)
		case r.Method == http.MethodDelete && r.URL.Path == "/installation/token":
			token := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
			if a.revokeErr != nil {
				if status := a.revokeErr(token); status != 0 {
					w.WriteHeader(status)
					if a.revokeMsg != "" {
						_ = json.NewEncoder(w).Encode(map[string]string{"message": fmt.Sprintf(a.revokeMsg, token)})
					}
					return
				}
			}
			a.revoked = append(a.revoked, token)
			w.WriteHeader(http.StatusNoContent)
		case r.Method == http.MethodGet && r.URL.Path == "/installation/repositories":
			if a.reposFail != 0 {
				w.WriteHeader(a.reposFail)
				return
			}
			token := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
			login := a.installs[a.installOf[token]-1]
			if names, ok := a.scoped[token]; ok {
				var items []string
				for _, name := range names {
					items = append(items, fmt.Sprintf(`{"full_name":"%s/%s"}`, login, name))
				}
				_, _ = fmt.Fprintf(w, `{"total_count":%d,"repository_selection":"selected","repositories":[%s]}`, len(items), strings.Join(items, ","))
				return
			}
			_, _ = fmt.Fprintf(w, `{"total_count":2,"repository_selection":"all","repositories":[{"full_name":"%s/two"},{"full_name":"%s/one"}]}`, login, login)
		case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/repos/"):
			a.repoPaths = append(a.repoPaths, r.URL.Path+" "+r.Header.Get("Authorization"))
			a.answerRepo(w, strings.TrimPrefix(r.URL.Path, "/repos/acme/app"))
		case r.Method == http.MethodGet && r.URL.Path == "/app":
			a.appAuth = append(a.appAuth, r.Header.Get("Authorization"))
			if a.slug == "" {
				w.WriteHeader(http.StatusNotFound)
				return
			}
			_, _ = fmt.Fprintf(w, `{"id":1234567,"slug":%q}`, a.slug)
		case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/users/"):
			a.userPath = r.URL.EscapedPath()
			a.userAuth = append(a.userAuth, r.Header.Get("Authorization"))
			if a.user == "" {
				w.WriteHeader(http.StatusNotFound)
				return
			}
			_, _ = w.Write([]byte(a.user))
		default:
			t.Errorf("unexpected %s %s", r.Method, r.URL)
		}
	}
}

func (a *api) answerRepo(w http.ResponseWriter, rest string) {
	repo := a.repo
	if repo == nil {
		repo = &fakeRepo{}
	}
	switch {
	case rest == "/installation" && repo.installed:
		_, _ = fmt.Fprintf(w, `{"id":%d,"account":{"login":"acme"}}`, slices.Index(a.installs, "acme")+1)
	case rest == "" && repo.installed:
		_, _ = fmt.Fprintf(w, `{"full_name":"acme/app","default_branch":%q}`, repo.defaultBranch)
	case strings.HasPrefix(rest, "/git/ref/") && repo.refStatus != 0:
		w.WriteHeader(repo.refStatus)
	case strings.HasPrefix(rest, "/git/ref/heads/") && slices.Contains(repo.branches, strings.TrimPrefix(rest, "/git/ref/heads/")),
		strings.HasPrefix(rest, "/git/ref/tags/") && slices.Contains(repo.tags, strings.TrimPrefix(rest, "/git/ref/tags/")):
		_, _ = fmt.Fprintf(w, `{"ref":%q}`, "refs/"+strings.TrimPrefix(rest, "/git/ref/"))
	default:
		w.WriteHeader(http.StatusNotFound)
	}
}

// revokedTokens is a copy of the fake's revocations, safe while its server runs.
func (a *api) revokedTokens() []string {
	a.mu.Lock()
	defer a.mu.Unlock()
	return slices.Clone(a.revoked)
}

type rig struct {
	g    GitHub
	keys *secrets
	api  *api
	exec *fakeExec
	srv  *httptest.Server
}

func newRig(t *testing.T, installs ...string) *rig {
	t.Helper()
	r := &rig{keys: &secrets{items: map[string][]byte{}, unreadable: map[string]bool{}}, api: &api{installs: installs}, exec: &fakeExec{}}
	r.srv = httptest.NewServer(r.api.handler(t))
	t.Cleanup(r.srv.Close)
	r.g = GitHub{
		Keychain: r.keys,
		Apps:     keys{account: "1234567", key: testAppKey(t)},
		API:      github.Client{Base: r.srv.URL, HTTP: r.srv.Client(), Now: func() time.Time { return clock }},
	}
	return r
}

// noKeychain fails t if anything but the App key reached the Keychain: GitHub tokens never do.
func (r *rig) noKeychain(t *testing.T) {
	t.Helper()
	if len(r.keys.calls) != 0 {
		t.Fatalf("keychain calls %q", r.keys.calls)
	}
}

// files is what r's exec wrote into the container: each token file's last value, by account.
func (r *rig) files() map[string]string {
	out := map[string]string{}
	for i, argv := range r.exec.argv {
		out[argv[len(argv)-1]] = string(r.exec.stdin[i])
	}
	return out
}

func wantCode(t *testing.T, err error, code protocol.Code) {
	t.Helper()
	var e *protocol.Error
	if !errors.As(err, &e) || e.Code != code {
		t.Fatalf("want %s, got %v", code, err)
	}
	if strings.Contains(err.Error(), "ghs_") {
		t.Fatalf("error leaks a token: %v", err)
	}
}

func TestAPIErrorIncludesGitHubMessage(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnprocessableEntity)
		_ = json.NewEncoder(w).Encode(map[string]string{"message": "Validation Failed", "token": "fake-ignored-credential"})
	}))
	defer srv.Close()
	c := github.Client{Base: srv.URL, HTTP: srv.Client()}
	_, _, err := c.Issue(context.Background(), "fake-request-credential", 42, perms)
	if err == nil {
		t.Fatal("expected error")
	}
	got := apiError(err)
	wantCode(t, got, protocol.GitHubFailed)
	if got.Message != `GitHub request failed: GitHub answered HTTP 422 to POST /app/installations/42/access_tokens: "Validation Failed".` {
		t.Fatalf("unexpected protocol error: %v", got)
	}
}

// A test App whose slug is not the key item's name.
const (
	slug        = "ensemble-test-bot"
	botUserJSON = `{"login":"ensemble-test-bot[bot]","id":5405554,"type":"Bot"}`
	savedUser   = `{"slug":"ensemble-test-bot","id":5405554}`
)

func TestReadyGitHub(t *testing.T) {
	r := newRig(t)
	wantCode(t, r.g.Ready(slug), protocol.LoginRequired)
	r.keys.items[usersService+"/"+AppKeyService] = []byte(savedUser)
	if err := r.g.Ready(slug); err != nil {
		t.Fatal(err)
	}
	err := r.g.Ready("ensemble-bot")
	wantCode(t, err, protocol.ManifestRejected)
	if err.(*protocol.Error).Message != "Manifest rejected: github app must be ensemble-test-bot, the App login github saved." {
		t.Fatalf("%v", err)
	}
	r.g.Apps = keys{err: ErrNotFound}
	err = r.g.Ready(slug)
	wantCode(t, err, protocol.LoginRequired)
	if !strings.Contains(err.Error(), "ensemble-broker login github ensemble-broker-bot") {
		t.Fatalf("message %v", err)
	}
	r.g.Apps = keys{account: "not-an-id", key: testAppKey(t)}
	wantCode(t, r.g.Ready(slug), protocol.LoginRequired)
}

func TestSaveUserSavesTheAppSlugAndBotUserID(t *testing.T) {
	r := newRig(t)
	r.api.slug, r.api.user = slug, botUserJSON
	var out strings.Builder
	if err := r.g.SaveUser(context.Background(), &out); err != nil {
		t.Fatal(err)
	}
	if got := string(r.keys.items[usersService+"/"+AppKeyService]); got != savedUser {
		t.Fatalf("saved %q", got)
	}
	if out.String() != "GitHub App ensemble-test-bot bot user ensemble-test-bot[bot] has ID 5405554\n" {
		t.Fatalf("output %q", out.String())
	}
	if r.api.userPath != "/users/ensemble-test-bot%5Bbot%5D" || !slices.Equal(r.api.userAuth, []string{""}) {
		t.Fatalf("lookup %s sent Authorization %q", r.api.userPath, r.api.userAuth)
	}
	if len(r.api.appAuth) != 1 || !strings.HasPrefix(r.api.appAuth[0], "Bearer ey") {
		t.Fatalf("GET /app sent %q, want the App JWT", r.api.appAuth)
	}
}

func TestSaveUserRejects(t *testing.T) {
	app := "looking up the GitHub App of the ensemble-broker-bot key failed: "
	lookup := "looking up GitHub user ensemble-test-bot[bot] failed: "
	rerun := "; rerun ensemble-broker login github ensemble-broker-bot"
	for name, c := range map[string]struct {
		slug, user, want string
		putErr           error
		down             bool
	}{
		"no app":      {"", botUserJSON, app + "GitHub answered HTTP 404 to GET /app" + rerun, nil, false},
		"absent":      {slug, "", lookup + "GitHub answered HTTP 404 to GET /users/ensemble-test-bot%5Bbot%5D" + rerun, nil, false},
		"unreachable": {slug, botUserJSON, app + "GitHub unreachable: GET /app" + rerun, nil, true},
		"not a bot":   {slug, `{"login":"ensemble-test-bot[bot]","id":1,"type":"User"}`, lookup + "GitHub answered no bot user with that login and a valid id" + rerun, nil, false},
		"save fails":  {slug, botUserJSON, "saving the bot user ID of ensemble-test-bot failed: " + errLeaky.Error(), errLeaky, false},
	} {
		r := newRig(t)
		r.api.slug, r.api.user, r.keys.putErr = c.slug, c.user, c.putErr
		if c.down {
			r.srv.Close()
		}
		var out strings.Builder
		err := r.g.SaveUser(context.Background(), &out)
		if err == nil || err.Error() != c.want {
			t.Errorf("%s: %v", name, err)
		}
		if _, ok := r.keys.items[usersService+"/"+AppKeyService]; ok || out.Len() != 0 {
			t.Errorf("%s: saved or printed %q", name, out.String())
		}
	}
	r := newRig(t)
	r.g.Apps = keys{err: ErrNotFound}
	wantCode(t, r.g.SaveUser(context.Background(), &strings.Builder{}), protocol.LoginRequired)
}

func TestBotUserID(t *testing.T) {
	r := newRig(t)
	item := usersService + "/" + AppKeyService
	r.keys.items[item] = []byte(savedUser)
	if id, err := r.g.BotUserID(slug); err != nil || id != 5405554 {
		t.Fatalf("%d %v", id, err)
	}
	wantCode(t, func() error { _, err := r.g.BotUserID("ensemble-bot"); return err }(), protocol.ManifestRejected)
	rerun := ": run ensemble-broker login github ensemble-broker-bot."
	for _, bad := range []string{"343764394", `{"slug":"ensemble-test-bot","id":0}`, `{"slug":"a b","id":1}`, "", "{"} {
		r.keys.items[item] = []byte(bad)
		_, err := r.g.BotUserID(slug)
		wantCode(t, err, protocol.LoginRequired)
		if err.(*protocol.Error).Message != "No GitHub App login"+rerun {
			t.Fatalf("%q: %v", bad, err)
		}
	}
	delete(r.keys.items, item)
	_, err := r.g.BotUserID(slug)
	wantCode(t, err, protocol.LoginRequired)
	r.keys.unreadable[usersService] = true
	_, err = r.g.BotUserID(slug)
	wantCode(t, err, protocol.KeychainFailed)
	if err.(*protocol.Error).Message != "Keychain access to the GitHub bot user ID failed: unlock the login keychain, or run "+RetrustCommand+"." {
		t.Fatalf("%v", err)
	}
}

// AC1: one token per installation, each in its own file, issued and audited, and the Keychain untouched.
func TestIssuePreparesEachInstallationAndNoKeychain(t *testing.T) {
	r := newRig(t, "acme", "Other-Org")
	commands, revoke, got, err := r.g.Issue(context.Background(), "ensemble-bot", perms, ctr)
	if err != nil || revoke == nil {
		t.Fatalf("revoke %v err %v", revoke == nil, err)
	}
	for _, command := range commands {
		defer command.Erase()
	}
	want := []protocol.Expiry{{Account: "Other-Org", ExpiresAt: "2026-10-17T13:00:00Z"}, {Account: "acme", ExpiresAt: "2026-10-17T13:00:00Z"}}
	if !slices.Equal(got, want) {
		t.Fatalf("expiries %v", got)
	}
	if len(commands) != 2 {
		t.Fatalf("execs %v", commands)
	}
	for i, account := range []string{"acme", "Other-Org"} {
		argv := commands[i].Argv
		if !slices.Equal(argv, []string{"/bin/sh", "-c", writeScript, "sh", account}) {
			t.Fatalf("argv %q", argv)
		}
		if string(commands[i].Stdin) != tok(i+1, i+1) {
			t.Fatalf("stdin %q", commands[i].Stdin)
		}
	}
	for _, part := range []string{"umask 0377", "mkdir -p -m 0700 " + docker.SecretsDir + "/github", `mv -f `} {
		if !strings.Contains(writeScript, part) {
			t.Errorf("script lacks %q", part)
		}
	}
	if !slices.Equal(r.api.bodies, []string{permsBody, permsBody}) || len(r.api.revoked) != 0 {
		t.Fatalf("token requests %q revoked %q", r.api.bodies, r.api.revoked)
	}
	r.noKeychain(t)
	revoke()
	revoke()
	if !slices.Equal(r.api.revoked, []string{tok(1, 1), tok(2, 2)}) {
		t.Fatalf("revoke sent %q, want each token once", r.api.revoked)
	}
	r.noKeychain(t)
}

func TestIssueNoInstallations(t *testing.T) {
	r := newRig(t)
	commands, revoke, got, err := r.g.Issue(context.Background(), "ensemble-bot", perms, ctr)
	if err != nil || got == nil || len(got) != 0 || len(commands) != 0 || revoke == nil {
		t.Fatalf("%v %v %v", got, err, commands)
	}
	revoke()
	if len(r.api.revoked) != 0 {
		t.Fatal(r.api.revoked)
	}
}

// AC5: a failure inside issuance revokes every token issued so far, once, even when the caller's context is gone.
func TestIssueFailureRevokesIssued(t *testing.T) {
	for name, tc := range map[string]struct {
		setup func(r *rig, cancel func())
		code  protocol.Code
		want  []string
	}{
		"second issue": {func(r *rig, _ func()) { r.api.issueFail = map[int]int{2: 500} }, protocol.GitHubFailed, []string{tok(1, 1)}},
		"cancelled": {func(r *rig, cancel func()) {
			r.api.issueFail = map[int]int{2: 500}
			r.api.onIssue = func(n int) {
				if n == 2 {
					cancel()
				}
			}
		}, "", []string{tok(1, 1)}},
	} {
		t.Run(name, func(t *testing.T) {
			r := newRig(t, "acme", "other")
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			tc.setup(r, cancel)
			commands, revoke, _, err := r.g.Issue(ctx, "ensemble-bot", perms, ctr)
			if err == nil || revoke != nil || len(commands) != 0 {
				t.Fatalf("err %v revoke %v", err, revoke != nil)
			}
			if tc.code != "" {
				wantCode(t, err, tc.code)
			}
			if strings.Contains(err.Error(), "ghs_") {
				t.Fatalf("error leaks a token: %v", err)
			}
			if !slices.Equal(r.api.revokedTokens(), tc.want) {
				t.Fatalf("revoked %q, want %q", r.api.revokedTokens(), tc.want)
			}
			r.noKeychain(t)
		})
	}
}

// An installation account that could not be a file name gets no token at all, nor does any other.
func TestIssueRefusesBadAccount(t *testing.T) {
	for _, account := range []string{"../claude", ".hidden", "a b"} {
		r := newRig(t, "acme", account)
		_, _, _, err := r.g.Issue(context.Background(), "ensemble-bot", perms, ctr)
		wantCode(t, err, protocol.GitHubFailed)
		if r.api.issued != 0 || len(r.api.revoked) != 0 || len(r.exec.argv) != 0 {
			t.Fatalf("%q: issued %d revoked %q writes %q", account, r.api.issued, r.api.revoked, r.exec.argv)
		}
	}
}

func TestIssueKeyProblems(t *testing.T) {
	r := newRig(t, "acme")
	r.g.Apps = keys{account: "1234567", key: []byte("garbage")}
	_, _, _, err := r.g.Issue(context.Background(), "ensemble-bot", perms, ctr)
	wantCode(t, err, protocol.GitHubFailed)
	r.g.Apps = keys{err: ErrNotFound}
	_, _, _, err = r.g.Issue(context.Background(), "ensemble-bot", perms, ctr)
	wantCode(t, err, protocol.LoginRequired)
	_, err = r.g.Refresh(context.Background(), "ensemble-bot", perms, ctr, r.exec.run)
	wantCode(t, err, protocol.LoginRequired)
	if r.api.issued != 0 {
		t.Fatal("issued without a key")
	}
}

func issued(t *testing.T, r *rig) []credential.Command {
	t.Helper()
	commands, _, _, err := r.g.Issue(context.Background(), "ensemble-bot", perms, ctr)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		for _, command := range commands {
			command.Erase()
		}
	})
	return commands
}

// AC2: a maintain with no record anywhere lists the App's installations now, one token each, over each file.
func TestRefreshWithoutAnyRecord(t *testing.T) {
	r := newRig(t, "acme", "other")
	issued(t, r)
	r.api.installs = []string{"acme", "", "added"} // other uninstalled, added installed since start
	r.exec.argv, r.exec.stdin = nil, nil
	got, err := r.g.Refresh(context.Background(), "ensemble-bot", map[string]string{"contents": "read"}, ctr, r.exec.run)
	want := []protocol.Expiry{{Account: "acme", ExpiresAt: "2026-10-17T13:00:00Z"}, {Account: "added", ExpiresAt: "2026-10-17T13:00:00Z"}}
	if err != nil || !slices.Equal(got, want) {
		t.Fatalf("%v %v", got, err)
	}
	if files := r.files(); !maps.Equal(files, map[string]string{"acme": tok(1, 3), "added": tok(3, 4)}) {
		t.Fatalf("files %v", files)
	}
	if body := `{"permissions":{"contents":"read"}}`; !slices.Equal(r.api.bodies[2:], []string{body, body}) {
		t.Fatalf("token requests %q, want the manifest's set", r.api.bodies)
	}
	r.noKeychain(t)
}

// AC3: maintain revokes nothing it replaced.
func TestRefreshRevokesNothingReplaced(t *testing.T) {
	r := newRig(t, "acme", "other")
	issued(t, r)
	for range 2 {
		if _, err := r.g.Refresh(context.Background(), "ensemble-bot", perms, ctr, r.exec.run); err != nil {
			t.Fatal(err)
		}
	}
	if len(r.api.revoked) != 0 || r.api.issued != 6 {
		t.Fatalf("revoked %q issued %d", r.api.revoked, r.api.issued)
	}
}

// AC3, D10: a renewed token whose write fails never reached the container: it alone is revoked; tokens written stay.
func TestRefreshRevokesTokenWhoseWriteFailed(t *testing.T) {
	r := newRig(t, "acme", "other")
	issued(t, r)
	ctx, cancel := context.WithCancel(context.Background())
	writes := 0
	exec := func(argv []string, stdin []byte) error {
		if writes++; writes == 2 { // the refresh's second write fails, its command cancelled meanwhile
			cancel()
			return errors.New("exec")
		}
		return r.exec.run(argv, stdin)
	}
	_, err := r.g.Refresh(ctx, "ensemble-bot", perms, ctr, exec)
	if err == nil || strings.Contains(err.Error(), "ghs_") {
		t.Fatalf("%v", err)
	}
	if got := r.api.revokedTokens(); !slices.Equal(got, []string{tok(2, 4)}) {
		t.Fatalf("revoked %q", got)
	}
	if files := r.files(); files["acme"] != tok(1, 3) {
		t.Fatalf("files %v", files)
	}
}

func TestRefreshIssueFails(t *testing.T) {
	r := newRig(t, "acme", "other")
	issued(t, r)
	r.api.issueFail = map[int]int{4: 502}
	_, err := r.g.Refresh(context.Background(), "ensemble-bot", perms, ctr, r.exec.run)
	wantCode(t, err, protocol.GitHubFailed)
	if len(r.api.revoked) != 0 || r.files()["acme"] != tok(1, 3) {
		t.Fatalf("revoked %q files %v", r.api.revoked, r.files())
	}
}

type roundTrip func(*http.Request) (*http.Response, error)

func (f roundTrip) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

// needsDialog is an App key item whose read would ask: one an earlier broker build wrote.
type needsDialog struct{ keys }

func (needsDialog) AppKey(string, string) ([]byte, error) { return nil, ErrNeedsDialog }

func TestJWTNeedsDialogNamesRetrust(t *testing.T) {
	g := GitHub{Apps: needsDialog{keys{account: "1234567"}}}
	_, err := g.jwt(AppKeyService)
	wantCode(t, err, protocol.KeychainFailed)
	if want := "The GitHub App private key ensemble-broker-bot needs keychain approval: unlock the login keychain, or run " + RetrustCommand + "."; err.(*protocol.Error).Message != want {
		t.Fatalf("%q", err.(*protocol.Error).Message)
	}
}
