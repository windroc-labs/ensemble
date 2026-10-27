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
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"ensemble/broker/internal/audit"
	"ensemble/broker/internal/credential"
	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/github"
	"ensemble/broker/internal/protocol"
)

const ctr = "ensemble-worker_1-0123456789abcdef0123456789abcdef"

// writeScript is the token file's atomic replacement, as every broker before the shared delivery wrote it.
const writeScript = "mkdir -p -m 0700 " + tokenDir + " && umask 0377 && rm -f " + tokenDir + `/."$1".new && cat > ` + tokenDir +
	`/."$1".new && mv -f ` + tokenDir + `/."$1".new ` + tokenDir + `/"$1"`

// The GitHub record and backlog shapes, as the lifecycle stores them.
type (
	tokenEntry  = credential.Material
	tokenRecord = credential.Record
	pending     = credential.Pending
)

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

// secrets is a fake broker-only keychain; unreadable makes a service's reads fail as a reinstalled broker's would.
type secrets struct {
	items      map[string][]byte
	unreadable map[string]bool
	putErr     error
}

func (s *secrets) Secret(service, account string) ([]byte, error) {
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
	if s.putErr != nil {
		return s.putErr
	}
	s.items[service+"/"+account] = slices.Clone(data)
	return nil
}

func (s *secrets) DeleteSecret(service, account string) error {
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
	revokeErr func(token string) int
	slug      string // GET /app's slug; "": 404
	appAuth   []string
	user      string // the bot user answer; "": 404
	userPath  string
	userAuth  []string
	reposFail int // the repository listing's status; 0: each installation reaches <login>/one and <login>/two
	onRepos   func(token string)
	bodies    []string // each token request's body
	repo      *fakeRepo
	repoPaths []string            // each /repos/ request's path and its token
	scoped    map[string][]string // each repository-limited token's repository names
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

func (a *api) handler(t *testing.T) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		a.mu.Lock()
		defer a.mu.Unlock()
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/app/installations":
			var items []string
			for i, login := range a.installs {
				items = append(items, fmt.Sprintf(`{"id":%d,"account":{"login":%q}}`, i+1, login))
			}
			_, _ = w.Write([]byte("[" + strings.Join(items, ",") + "]"))
		case r.Method == http.MethodPost && strings.HasPrefix(r.URL.Path, "/app/installations/"):
			body, _ := io.ReadAll(r.Body)
			a.bodies = append(a.bodies, string(body))
			a.issued++
			if status := a.issueFail[a.issued]; status != 0 {
				w.WriteHeader(status)
				return
			}
			id := strings.TrimSuffix(strings.TrimPrefix(r.URL.Path, "/app/installations/"), "/access_tokens")
			token := fmt.Sprintf("ghs_%s_%d", id, a.issued)
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
					return
				}
			}
			a.revoked = append(a.revoked, token)
			w.WriteHeader(http.StatusNoContent)
		case r.Method == http.MethodGet && r.URL.Path == "/installation/repositories":
			if a.onRepos != nil {
				a.onRepos(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))
			}
			if a.reposFail != 0 {
				w.WriteHeader(a.reposFail)
				return
			}
			token := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
			var id int
			_, _ = fmt.Sscanf(strings.TrimPrefix(token, "ghs_"), "%d_", &id)
			login := a.installs[id-1]
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
		Lock:     FileLock(filepath.Join(t.TempDir(), "tokens.lock")),
	}
	return r
}

func (r *rig) record(t *testing.T) (rec tokenRecord, ok bool) {
	t.Helper()
	data, found := r.keys.items[tokensService+"/"+ctr]
	if !found {
		return rec, false
	}
	if err := json.Unmarshal(data, &rec); err != nil {
		t.Fatal(err)
	}
	return rec, true
}

func (r *rig) backlog(t *testing.T) []pending {
	t.Helper()
	var list []pending
	if data, ok := r.keys.items[revocationsService+"/"+revocationsAccount]; ok {
		if err := json.Unmarshal(data, &list); err != nil {
			t.Fatal(err)
		}
	}
	return list
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

func TestIssueWritesFiles(t *testing.T) {
	r := newRig(t, "acme", "Other-Org")
	got, err := r.g.Issue(context.Background(), "ensemble-bot", "", perms, ctr, r.exec.run)
	if err != nil {
		t.Fatal(err)
	}
	want := []protocol.Expiry{{Account: "Other-Org", ExpiresAt: "2026-10-17T13:00:00Z"}, {Account: "acme", ExpiresAt: "2026-10-17T13:00:00Z"}}
	if !slices.Equal(got, want) {
		t.Fatalf("expiries %v", got)
	}
	if len(r.exec.argv) != 2 {
		t.Fatalf("execs %v", r.exec.argv)
	}
	for i, account := range []string{"acme", "Other-Org"} {
		argv := r.exec.argv[i]
		if argv[0] != "/bin/sh" || argv[1] != "-c" || argv[2] != writeScript || argv[3] != "sh" || argv[4] != account || len(argv) != 5 {
			t.Fatalf("argv %q", argv)
		}
		if string(r.exec.stdin[i]) != fmt.Sprintf("ghs_%d_%d", i+1, i+1) {
			t.Fatalf("stdin %q", r.exec.stdin[i])
		}
		if strings.Contains(strings.Join(argv, " "), "ghs_") {
			t.Fatal("token in argv")
		}
	}
	for _, part := range []string{"umask 0377", "mkdir -p -m 0700 " + docker.SecretsDir + "/github", `mv -f `} {
		if !strings.Contains(writeScript, part) {
			t.Errorf("script lacks %q", part)
		}
	}
	if !slices.Equal(r.api.bodies, []string{permsBody, permsBody}) {
		t.Fatalf("token requests %q", r.api.bodies)
	}
	rec, ok := r.record(t)
	if !ok || rec.Subject != "ensemble-bot" || len(rec.Tokens) != 2 || rec.Tokens[0] != (tokenEntry{Account: "acme", Target: 1, Token: "ghs_1_1", ExpiresAt: "2026-10-17T13:00:00Z", Revision: 1, State: credential.Delivered}) {
		t.Fatalf("record %v", rec)
	}
}

func TestIssueRecordsBeforeWrite(t *testing.T) {
	r := newRig(t, "acme")
	var recorded bool
	exec := func(argv []string, stdin []byte) error {
		_, recorded = r.record(t)
		return nil
	}
	if _, err := r.g.Issue(context.Background(), "ensemble-bot", "", perms, ctr, exec); err != nil || !recorded {
		t.Fatalf("recorded %v err %v", recorded, err)
	}
}

func TestIssueNoInstallations(t *testing.T) {
	r := newRig(t)
	got, err := r.g.Issue(context.Background(), "ensemble-bot", "", perms, ctr, r.exec.run)
	if err != nil || len(got) != 0 || len(r.exec.argv) != 0 {
		t.Fatalf("%v %v %v", got, err, r.exec.argv)
	}
	if _, ok := r.record(t); ok {
		t.Fatal("empty record kept")
	}
}

func TestIssueFailureRevokes(t *testing.T) {
	for name, tc := range map[string]struct {
		setup func(r *rig)
		code  protocol.Code
	}{
		"second issue": {func(r *rig) { r.api.issueFail = map[int]int{2: 500} }, protocol.GitHubFailed},
		"write":        {func(r *rig) { r.exec.err = errors.New("exec") }, ""},
	} {
		t.Run(name, func(t *testing.T) {
			r := newRig(t, "acme", "other")
			tc.setup(r)
			_, err := r.g.Issue(context.Background(), "ensemble-bot", "", perms, ctr, r.exec.run)
			if err == nil {
				t.Fatal("no error")
			}
			if tc.code != "" {
				wantCode(t, err, tc.code)
			}
			if _, ok := r.record(t); ok {
				t.Fatal("record kept")
			}
			if len(r.backlog(t)) != 0 || !slices.Contains(r.api.revoked, "ghs_1_1") {
				t.Fatalf("revoked %v backlog %v", r.api.revoked, r.backlog(t))
			}
		})
	}
}

func TestIssueRollbackOffline(t *testing.T) {
	r := newRig(t, "acme", "other")
	r.api.issueFail = map[int]int{2: 500}
	r.api.revokeErr = func(string) int { return 503 }
	_, err := r.g.Issue(context.Background(), "ensemble-bot", "", perms, ctr, r.exec.run)
	wantCode(t, err, protocol.GitHubFailed)
	if b := r.backlog(t); len(b) != 1 || b[0] != (pending{Container: ctr, Account: "acme", Token: "ghs_1_1", ExpiresAt: "2026-10-17T13:00:00Z"}) {
		t.Fatalf("backlog %v", b)
	}
}

// lockFails makes every token lock after the first ok ones fail.
func (r *rig) lockFails(ok int) {
	lock, n := r.g.Lock, 0
	r.g.Lock = func() (func(), error) {
		if n++; n > ok {
			return nil, errors.New("flock: input/output error")
		}
		return lock()
	}
}

func TestLockFailureRevokesTheNewToken(t *testing.T) {
	for name, run := range map[string]func(r *rig) error{
		"issue": func(r *rig) error {
			r.lockFails(0)
			_, err := r.g.Issue(context.Background(), "ensemble-bot", "", perms, ctr, r.exec.run)
			return err
		},
		"refresh": func(r *rig) error {
			issued(t, r)
			r.lockFails(1)
			_, err := r.g.Refresh(context.Background(), "ensemble-bot", "", perms, ctr, r.exec.run)
			return err
		},
	} {
		t.Run(name, func(t *testing.T) {
			r := newRig(t, "acme")
			err := run(r)
			wantCode(t, err, protocol.KeychainFailed)
			if !strings.Contains(err.Error(), "token lock") {
				t.Fatalf("unclear error: %v", err)
			}
			if !slices.Contains(r.api.revoked, "ghs_1_"+strconv.Itoa(r.api.issued)) || len(r.exec.argv) != 0 {
				t.Fatalf("revoked %v writes %v", r.api.revoked, r.exec.argv)
			}
		})
	}
}

func TestLockFailureQueuesAnUnrevokedToken(t *testing.T) {
	r := newRig(t, "acme")
	r.lockFails(0)
	r.api.revokeErr = func(string) int { return 503 }
	_, err := r.g.Issue(context.Background(), "ensemble-bot", "", perms, ctr, r.exec.run)
	wantCode(t, err, protocol.KeychainFailed)
	if b := r.backlog(t); len(b) != 1 || b[0].Token != "ghs_1_1" {
		t.Fatalf("backlog %v", b)
	}
}

func TestRevokeLockFails(t *testing.T) {
	r := newRig(t, "acme", "other")
	issued(t, r)
	r.lockFails(0)
	if err := r.g.Release(context.Background(), ctr); err == nil || !strings.Contains(err.Error(), "token lock") {
		t.Fatalf("unclear error: %v", err)
	}
	if !slices.Equal(r.api.revoked, []string{"ghs_1_1", "ghs_2_2"}) {
		t.Fatalf("revoked %v", r.api.revoked)
	}
	if _, ok := r.record(t); ok {
		t.Fatal("stale record of revoked tokens kept")
	}
}

func TestRevokeLockFailsKeepsATokenRecordedMeanwhile(t *testing.T) {
	r := newRig(t, "acme")
	issued(t, r)
	r.lockFails(0)
	r.api.revokeErr = func(string) int { // a refresh records its new token while close revokes without the lock
		rec, _ := r.record(t)
		if !slices.ContainsFunc(rec.Tokens, func(e tokenEntry) bool { return e.Token == "ghs_late" }) {
			rec.Tokens = append(rec.Tokens, tokenEntry{Account: "acme", Target: 1, Token: "ghs_late", ExpiresAt: "2026-10-17T13:00:00Z"})
			data, _ := json.Marshal(rec)
			r.keys.items[tokensService+"/"+ctr] = data
		}
		return 0
	}
	_ = r.g.Release(context.Background(), ctr)
	rec, ok := r.record(t)
	if !ok || len(rec.Tokens) != 1 || rec.Tokens[0].Token != "ghs_late" {
		t.Fatalf("record %v %v", ok, rec.Tokens)
	}
}

func TestIssueLockFailureLeavesNoRecord(t *testing.T) {
	r := newRig(t, "acme", "other")
	r.lockFails(1)
	_, err := r.g.Issue(context.Background(), "ensemble-bot", "", perms, ctr, r.exec.run)
	wantCode(t, err, protocol.KeychainFailed)
	if _, ok := r.record(t); ok || !slices.Contains(r.api.revoked, "ghs_1_1") {
		t.Fatalf("record kept or revoked %v", r.api.revoked)
	}
}

func TestRevokeReadsTheRecordUnderTheLock(t *testing.T) {
	r := newRig(t, "acme")
	lock, n := r.g.Lock, 0
	r.g.Lock = func() (func(), error) {
		if n++; n == 1 { // a refresh's record write lands just before close takes the lock
			data, _ := json.Marshal(tokenRecord{Subject: "ensemble-bot", Tokens: []tokenEntry{{Account: "acme", Target: 1, Token: "ghs_late", ExpiresAt: "2026-10-17T13:00:00Z"}}})
			r.keys.items[tokensService+"/"+ctr] = data
		}
		return lock()
	}
	if err := r.g.Release(context.Background(), ctr); err != nil {
		t.Fatal(err)
	}
	if _, ok := r.record(t); ok || !slices.Contains(r.api.revoked, "ghs_late") {
		t.Fatalf("record kept or revoked %v", r.api.revoked)
	}
}

func TestIssueKeyProblems(t *testing.T) {
	r := newRig(t, "acme")
	r.g.Apps = keys{account: "1234567", key: []byte("garbage")}
	_, err := r.g.Issue(context.Background(), "ensemble-bot", "", perms, ctr, r.exec.run)
	wantCode(t, err, protocol.GitHubFailed)
	r.g.Apps = keys{err: ErrNotFound}
	_, err = r.g.Issue(context.Background(), "ensemble-bot", "", perms, ctr, r.exec.run)
	wantCode(t, err, protocol.LoginRequired)
	if r.api.issued != 0 {
		t.Fatal("issued without a key")
	}
}

func issued(t *testing.T, r *rig) {
	t.Helper()
	if _, err := r.g.Issue(context.Background(), "ensemble-bot", "", perms, ctr, r.exec.run); err != nil {
		t.Fatal(err)
	}
	r.exec.argv, r.exec.stdin = nil, nil
}

func TestRefresh(t *testing.T) {
	r := newRig(t, "acme")
	issued(t, r)
	got, err := r.g.Refresh(context.Background(), "ensemble-bot", "", perms, ctr, r.exec.run)
	if err != nil || !slices.Equal(got, []protocol.Expiry{{Account: "acme", ExpiresAt: "2026-10-17T13:00:00Z"}}) {
		t.Fatalf("%v %v", got, err)
	}
	if len(r.exec.argv) != 1 || string(r.exec.stdin[0]) != "ghs_1_2" {
		t.Fatalf("writes %q", r.exec.stdin)
	}
	if !slices.Equal(r.api.bodies, []string{permsBody, permsBody}) {
		t.Fatalf("token requests %q (the refresh's must carry the set)", r.api.bodies)
	}
	if !slices.Equal(r.api.revoked, []string{"ghs_1_1"}) {
		t.Fatalf("revoked %v (only the record's old value)", r.api.revoked)
	}
	rec, _ := r.record(t)
	if len(rec.Tokens) != 1 || rec.Tokens[0].Token != "ghs_1_2" {
		t.Fatalf("record %v", rec)
	}
}

func TestRefreshNoRecord(t *testing.T) {
	r := newRig(t, "acme")
	_, err := r.g.Refresh(context.Background(), "ensemble-bot", "", perms, ctr, r.exec.run)
	wantCode(t, err, protocol.RefreshFailed)
	if r.api.issued != 0 {
		t.Fatal("issued")
	}
}

func TestRefreshRecordGone(t *testing.T) {
	r := newRig(t, "acme")
	issued(t, r)
	r.api.revokeErr = func(token string) int {
		return 0
	}
	// a close takes the record away while refresh asks GitHub
	srv := r.g.API
	r.g.API.HTTP = &http.Client{Transport: roundTrip(func(req *http.Request) (*http.Response, error) {
		if req.Method == http.MethodPost {
			delete(r.keys.items, tokensService+"/"+ctr)
		}
		return srv.HTTP.Transport.RoundTrip(req)
	})}
	_, err := r.g.Refresh(context.Background(), "ensemble-bot", "", perms, ctr, r.exec.run)
	wantCode(t, err, protocol.RefreshFailed)
	if len(r.exec.argv) != 0 || !slices.Contains(r.api.revoked, "ghs_1_2") {
		t.Fatalf("writes %v revoked %v", r.exec.argv, r.api.revoked)
	}
	if _, ok := r.record(t); ok {
		t.Fatal("record recreated")
	}
}

type roundTrip func(*http.Request) (*http.Response, error)

func (f roundTrip) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestRefreshWriteFails(t *testing.T) {
	r := newRig(t, "acme")
	issued(t, r)
	r.exec.err = errors.New("gone")
	if _, err := r.g.Refresh(context.Background(), "ensemble-bot", "", perms, ctr, r.exec.run); err == nil {
		t.Fatal("no error")
	}
	rec, _ := r.record(t)
	if len(rec.Tokens) != 1 || rec.Tokens[0].Token != "ghs_1_1" || !slices.Equal(r.api.revoked, []string{"ghs_1_2"}) {
		t.Fatalf("record %v revoked %v", rec, r.api.revoked)
	}
}

func TestRefreshOldRevokeFails(t *testing.T) {
	r := newRig(t, "acme")
	issued(t, r)
	r.api.revokeErr = func(token string) int {
		if token == "ghs_1_1" {
			return 502
		}
		return 0
	}
	if _, err := r.g.Refresh(context.Background(), "ensemble-bot", "", perms, ctr, r.exec.run); err != nil {
		t.Fatal(err)
	}
	rec, _ := r.record(t)
	if b := r.backlog(t); len(b) != 1 || b[0].Token != "ghs_1_1" || len(rec.Tokens) != 1 || rec.Tokens[0].Token != "ghs_1_2" {
		t.Fatalf("backlog %v record %v", b, rec)
	}
}

func TestRevoke(t *testing.T) {
	r := newRig(t, "acme", "other")
	issued(t, r)
	if err := r.g.Release(context.Background(), ctr); err != nil {
		t.Fatal(err)
	}
	if len(r.exec.argv) != 0 {
		t.Fatal("revoke ran in the container")
	}
	if !slices.Equal(r.api.revoked, []string{"ghs_1_1", "ghs_2_2"}) {
		t.Fatalf("revoked %v", r.api.revoked)
	}
	if _, ok := r.record(t); ok || len(r.backlog(t)) != 0 {
		t.Fatal("left state")
	}
	if err := r.g.Release(context.Background(), ctr); err != nil {
		t.Fatalf("absent record: %v", err)
	}
}

func TestRevokeTakesATokenRecordedMeanwhile(t *testing.T) {
	r := newRig(t, "acme")
	issued(t, r)
	lock, n := r.g.Lock, 0
	r.g.Lock = func() (func(), error) {
		if n++; n == 2 { // a refresh records its new token between close's lock sections
			rec, _ := r.record(t)
			rec.Tokens = append(rec.Tokens, tokenEntry{Account: "acme", Target: 1, Token: "ghs_late", ExpiresAt: rec.Tokens[0].ExpiresAt})
			data, _ := json.Marshal(rec)
			r.keys.items[tokensService+"/"+ctr] = data
		}
		return lock()
	}
	if err := r.g.Release(context.Background(), ctr); err != nil {
		t.Fatal(err)
	}
	if !slices.Contains(r.api.revoked, "ghs_late") || len(r.backlog(t)) != 0 {
		t.Fatalf("revoked %v backlog %v", r.api.revoked, r.backlog(t))
	}
}

func TestRevokeOffline(t *testing.T) {
	r := newRig(t, "acme", "other")
	issued(t, r)
	r.api.revokeErr = func(string) int { return 0 }
	r.srv.Close()
	if err := r.g.Release(context.Background(), ctr); err != nil {
		t.Fatal(err)
	}
	if b := r.backlog(t); len(b) != 2 || b[0].Container != ctr {
		t.Fatalf("backlog %v", b)
	}
	if _, ok := r.record(t); ok {
		t.Fatal("record kept")
	}
}

func TestRetryRevocations(t *testing.T) {
	r := newRig(t)
	list := []pending{
		{Container: "c1", Account: "a", Token: "ghs_expired", ExpiresAt: "2026-10-17T11:59:59Z"},
		{Container: "c1", Account: "b", Token: "ghs_live", ExpiresAt: "2026-10-17T12:30:00Z"},
		{Container: "c2", Account: "c", Token: "ghs_down", ExpiresAt: "2026-10-17T12:30:00Z"},
		{Container: "c2", Account: "d", Token: "ghs_after", ExpiresAt: "2026-10-17T12:30:00Z"},
	}
	data, _ := json.Marshal(list)
	r.keys.items[revocationsService+"/"+revocationsAccount] = data
	var calls []string
	r.g.API.HTTP = &http.Client{Transport: roundTrip(func(req *http.Request) (*http.Response, error) {
		token := strings.TrimPrefix(req.Header.Get("Authorization"), "Bearer ")
		calls = append(calls, token)
		if token == "ghs_down" {
			return nil, errors.New("dial tcp: timeout")
		}
		return &http.Response{StatusCode: 204, Body: http.NoBody}, nil
	})}
	got, err := r.g.Retry(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(calls, []string{"ghs_live", "ghs_down"}) {
		t.Fatalf("calls %v: expired skipped, stop at the first transport error", calls)
	}
	want := []protocol.Pending{{Container: "c2", Account: "c", ExpiresAt: "2026-10-17T12:30:00Z"}, {Container: "c2", Account: "d", ExpiresAt: "2026-10-17T12:30:00Z"}}
	if !slices.Equal(got, want) {
		t.Fatalf("pending %v", got)
	}
	if b := r.backlog(t); len(b) != 2 {
		t.Fatalf("backlog %v", b)
	}
}

func TestUnreadableBacklog(t *testing.T) {
	r := newRig(t, "acme")
	issued(t, r)
	r.keys.unreadable[revocationsService] = true
	_, err := r.g.Retry(context.Background())
	wantCode(t, err, protocol.KeychainFailed)
	if err := r.g.Release(context.Background(), ctr); err != nil {
		t.Fatalf("readable record revoked directly: %v", err)
	}
	if !slices.Equal(r.api.revoked, []string{"ghs_1_1"}) {
		t.Fatalf("revoked %v", r.api.revoked)
	}
	issued(t, r)
	r.api.revokeErr = func(string) int { return 500 }
	wantCode(t, r.g.Release(context.Background(), ctr), protocol.KeychainFailed)
}

func TestUnreadableRecord(t *testing.T) {
	r := newRig(t, "acme")
	issued(t, r)
	r.keys.unreadable[tokensService] = true
	err := r.g.Release(context.Background(), ctr)
	wantCode(t, err, protocol.KeychainFailed)
	if !strings.Contains(err.Error(), "expire within an hour") {
		t.Fatalf("message %v", err)
	}
	if _, ok := r.keys.items[tokensService+"/"+ctr]; !ok {
		t.Fatal("unreadable recovery record was deleted")
	}
	r.keys.unreadable[tokensService] = false
	rebuilt := GitHub{Keychain: r.keys, API: r.g.API, Lock: r.g.Lock}
	if err := rebuilt.Release(context.Background(), ctr); err != nil || !slices.Contains(r.api.revoked, "ghs_1_1") {
		t.Fatalf("restored record could not be recovered: %v", err)
	}
	r.keys.unreadable[tokensService] = true
	r.keys.items[tokensService+"/"+ctr] = []byte("{}")
	_, err = r.g.Refresh(context.Background(), "ensemble-bot", "", perms, ctr, r.exec.run)
	wantCode(t, err, protocol.KeychainFailed)
}

func TestFileLock(t *testing.T) {
	path := filepath.Join(t.TempDir(), "sub", "tokens.lock")
	unlock, err := FileLock(path)()
	if err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(path)
	if err != nil || info.Mode().Perm() != 0o600 {
		t.Fatalf("%v %v", info, err)
	}
	unlock()
	unlock, err = FileLock(path)()
	if err != nil {
		t.Fatal(err)
	}
	unlock()
}

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

func TestTokensStoreErrorNamesRetrust(t *testing.T) {
	err := tokensError("ensemble-w-0123", fmt.Errorf("records: %w", credential.ErrStore))
	wantCode(t, err, protocol.KeychainFailed)
	if want := "Keychain access to the GitHub tokens failed: unlock the login keychain, or run " + RetrustCommand + "."; err.(*protocol.Error).Message != want {
		t.Fatalf("%q", err.(*protocol.Error).Message)
	}
}

// repoBody is a repo grant's token request for acme/app with perms.
const repoBody = `{"permissions":{"contents":"write","statuses":"read","workflows":"write"},"repositories":["app"]}`

// repoRig is a rig whose App is installed on other and acme, acme/app among acme's repositories, auditing a start.
func repoRig(t *testing.T, installed bool) (*rig, audited) {
	t.Helper()
	r := newRig(t, "other", "acme")
	r.api.repo = &fakeRepo{installed: installed}
	rec, sink := r.audited("start", ctr)
	return r, audited{rec, sink}
}

func TestIssueRepoWritesOneFile(t *testing.T) {
	r, a := repoRig(t, true)
	got, err := r.g.Issue(context.Background(), "ensemble-bot", "acme/app", perms, ctr, r.exec.run)
	if err != nil || !slices.Equal(got, []protocol.Expiry{{Account: "acme", ExpiresAt: "2026-10-17T13:00:00Z"}}) {
		t.Fatalf("%v %v", got, err)
	}
	if len(r.api.repoPaths) != 1 || !strings.HasPrefix(r.api.repoPaths[0], "/repos/acme/app/installation Bearer ey") {
		t.Fatalf("repo requests %q", r.api.repoPaths)
	}
	if !slices.Equal(r.api.bodies, []string{repoBody}) {
		t.Fatalf("token requests %q", r.api.bodies)
	}
	if len(r.exec.argv) != 1 || !slices.Equal(r.exec.argv[0], []string{"/bin/sh", "-c", writeScript, "sh", "acme"}) || string(r.exec.stdin[0]) != "ghs_2_1" {
		t.Fatalf("writes %q", r.exec.argv)
	}
	rec, ok := r.record(t)
	want := tokenEntry{Account: "acme", Target: 2, Token: "ghs_2_1", ExpiresAt: "2026-10-17T13:00:00Z", Revision: 1, State: credential.Delivered, Repo: "acme/app"}
	if !ok || rec.Subject != "ensemble-bot" || len(rec.Tokens) != 1 || rec.Tokens[0] != want {
		t.Fatalf("record %+v", rec)
	}
	lines := flush(t, a.rec, a.sink)
	if len(lines) != 1 || len(lines[0].Credentials) != 1 {
		t.Fatalf("%+v", lines)
	}
	if c := lines[0].Credentials[0]; c.Result != audit.Issued || c.Account != "acme" || c.Selection == nil || *c.Selection != "selected" ||
		c.Repositories == nil || !slices.Equal(*c.Repositories, []string{"acme/app"}) {
		t.Fatalf("audit %+v", c)
	}
}

func TestIssueRepoNotInstalled(t *testing.T) {
	r, _ := repoRig(t, false)
	_, err := r.g.Issue(context.Background(), "ensemble-bot", "acme/app", perms, ctr, r.exec.run)
	wantCode(t, err, protocol.GitHubFailed)
	if msg := err.(*protocol.Error).Message; msg != "GitHub App ensemble-broker-bot is not installed on acme/app; install it there." {
		t.Fatalf("message %q", msg)
	}
	if r.api.issued != 0 || len(r.exec.argv) != 0 {
		t.Fatalf("issued %d writes %q", r.api.issued, r.exec.argv)
	}
	if _, ok := r.record(t); ok {
		t.Fatal("record kept")
	}
}

// A renewal issues for the recorded repo with the recorded set, whatever the caller's set.
func TestRefreshRepoKeepsTheRepo(t *testing.T) {
	r, _ := repoRig(t, true)
	if _, err := r.g.Issue(context.Background(), "ensemble-bot", "acme/app", perms, ctr, r.exec.run); err != nil {
		t.Fatal(err)
	}
	r.exec.argv, r.exec.stdin = nil, nil
	got, err := r.g.Refresh(context.Background(), "ensemble-bot", "acme/app", map[string]string{"contents": "read"}, ctr, r.exec.run)
	if err != nil || !slices.Equal(got, []protocol.Expiry{{Account: "acme", ExpiresAt: "2026-10-17T13:00:00Z"}}) {
		t.Fatalf("%v %v", got, err)
	}
	if !slices.Equal(r.api.bodies, []string{repoBody, repoBody}) {
		t.Fatalf("token requests %q", r.api.bodies)
	}
	if len(r.exec.argv) != 1 || r.exec.argv[0][4] != "acme" || string(r.exec.stdin[0]) != "ghs_2_2" || !slices.Equal(r.api.revoked, []string{"ghs_2_1"}) {
		t.Fatalf("writes %q revoked %q", r.exec.argv, r.api.revoked)
	}
	rec, _ := r.record(t)
	if len(rec.Tokens) != 1 || rec.Tokens[0].Token != "ghs_2_2" || rec.Tokens[0].Repo != "acme/app" || rec.Tokens[0].Target != 2 {
		t.Fatalf("record %+v", rec)
	}
}

// A repo grant renews only its own slot: one recorded for another repository (here a prefetch token whose revocation
// and backlog write both failed) gets no token request, and the renewal fails naming it.
func TestRefreshRepoRefusesASlotOutsideTheGrant(t *testing.T) {
	r, _ := repoRig(t, true)
	held := tokenEntry{Account: "other", Target: 1, Token: "ghs_held", ExpiresAt: "2026-10-17T13:00:00Z", Revision: 1, State: credential.Prepared, Repo: "other/src"}
	data, _ := json.Marshal(tokenRecord{Subject: "ensemble-bot", Tokens: []tokenEntry{held}})
	r.keys.items[tokensService+"/"+ctr] = data
	if _, err := r.g.Issue(context.Background(), "ensemble-bot", "acme/app", perms, ctr, r.exec.run); err != nil {
		t.Fatal(err)
	}
	r.exec.argv, r.exec.stdin = nil, nil
	_, err := r.g.Refresh(context.Background(), "ensemble-bot", "acme/app", perms, ctr, r.exec.run)
	wantCode(t, err, protocol.RefreshFailed)
	if msg := err.(*protocol.Error).Message; msg != "The GitHub token of other for other/src is outside the repo grant acme/app; not renewed." {
		t.Fatalf("message %q", msg)
	}
	if r.api.issued != 1 || len(r.exec.argv) != 0 {
		t.Fatalf("issued %d writes %q", r.api.issued, r.exec.argv)
	}
	if rec, _ := r.record(t); len(rec.Tokens) != 2 || rec.Tokens[0] != held {
		t.Fatalf("record %+v", rec)
	}
}

// The token a prefetch holds while cloning records the one repository it reaches.
func TestPrefetchHoldsItsRepo(t *testing.T) {
	r, _ := prefetchRig(t, installed())
	var during []tokenEntry
	run := func(argv []string, stdin []byte) error {
		rec, _ := r.record(t)
		during = rec.Tokens
		return r.exec.run(argv, stdin)
	}
	if err := r.g.Prefetch(context.Background(), "acme/app", "main", "/w", ctr, run); err != nil {
		t.Fatal(err)
	}
	if len(during) != 1 || during[0].Token != "ghs_2_1" || during[0].Repo != "acme/app" {
		t.Fatalf("held %+v", during)
	}
}

func TestRevokeRepoGrant(t *testing.T) {
	r, _ := repoRig(t, true)
	if _, err := r.g.Issue(context.Background(), "ensemble-bot", "acme/app", perms, ctr, r.exec.run); err != nil {
		t.Fatal(err)
	}
	rec, sink := r.audited("close", ctr)
	if err := r.g.Release(context.Background(), ctr); err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(r.api.revoked, []string{"ghs_2_1"}) {
		t.Fatalf("revoked %q", r.api.revoked)
	}
	if _, ok := r.record(t); ok || len(r.backlog(t)) != 0 {
		t.Fatal("left state")
	}
	lines := flush(t, rec, sink)
	if len(lines) != 1 || !slices.Contains(resultsOf(lines[0]), audit.Revoked) || lines[0].Credentials[len(lines[0].Credentials)-1].Account != "acme" {
		t.Fatalf("audit %+v", lines)
	}
}
