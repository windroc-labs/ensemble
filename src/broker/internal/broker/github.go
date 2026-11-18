package broker

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"ensemble/broker/internal/audit"
	"ensemble/broker/internal/credential"
	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/github"
	"ensemble/broker/internal/launcher"
	"ensemble/broker/internal/protocol"
)

// Installation tokens are never stored: only the container's files hold them.
const (
	usersService = "ensemble-broker-github-users"
	tokenDir     = docker.GitHubDir
)

// HostContainer is the host's gh, not a container.
const HostContainer = "host/gh"

var hostPermissions = map[string]string{"contents": "write", "pull_requests": "write", "workflows": "write",
	"checks": "read", "actions": "read", "statuses": "read", "metadata": "read"}

// revocationTimeout bounds each step that must end whatever the caller's context.
var revocationTimeout = 30 * time.Second

// cloneScript: git reads the token through a command-line credential helper, so the clone's config and remote URL
// never hold it.
const cloneScript = `set -eu
umask 077
cat > "$HOME/token"
exec git -c core.hooksPath=/dev/null -c core.fsmonitor=false -c credential.helper= ` +
	`-c 'credential.helper=!f() { test "$1" = get || exit 0; echo username=x-access-token; printf "password=%s\n" "$(cat "$HOME/token")"; }; f' ` +
	`clone --quiet --depth 1 --branch "$2" -- "https://github.com/$1.git" "$3"`

// tokensScript follows no symlink, the directory's own included.
const tokensScript = `[ -d "$1" ] || exit 0
find "$1" -maxdepth 1 -type f ! -name '.*' | head -n 100 | while IFS= read -r f; do
	if [ -f "$f" ] && [ ! -L "$f" ]; then
		printf '%s %s %s\n' "${f##*/}" "$(stat -c %Y "$f")" "$(head -c 256 "$f")"
	fi
done`

const maxTokenFiles = 100

var (
	installationToken = regexp.MustCompile(`^ghs_[A-Za-z0-9]+$`)
	decimal           = regexp.MustCompile(`^[0-9]{1,18}$`)
)

var prefetchPermissions = map[string]string{"contents": "read"}

type botUser struct {
	Slug string `json:"slug"`
	ID   int64  `json:"id"`
}

type AppKeys interface {
	AppAccount(app string) (string, error)
	AppKey(app, account string) ([]byte, error)
}

// GitHub keeps no copy of a token: it is revoked by the command that issued it or read back at close, else expires
// within the hour.
type GitHub struct {
	Keychain SecretKeychain
	Apps     AppKeys
	API      github.Client
	Audit    *audit.Recorder
}

func FileLock(path string, create bool) func() (func(), error) {
	return func() (func(), error) {
		flags := os.O_RDWR
		if create {
			if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
				return nil, err
			}
			flags |= os.O_CREATE
		}
		f, err := os.OpenFile(path, flags, 0o600)
		if err != nil {
			return nil, err
		}
		return lockFile(f)
	}
}

func lockFile(f *os.File) (func(), error) {
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX); err != nil {
		f.Close()
		return nil, err
	}
	// A waiting opener may hold an inode retired by container close.
	held, err := f.Stat()
	if err == nil {
		var current os.FileInfo
		current, err = os.Stat(f.Name())
		if err == nil && !os.SameFile(held, current) {
			err = os.ErrNotExist
		}
	}
	if err != nil {
		f.Close()
		return nil, err
	}
	return func() { f.Close() }, nil
}

func apiError(err error) *protocol.Error {
	return protocol.New(protocol.GitHubFailed, "GitHub request failed: "+err.Error()+".")
}

func loginError(app string) *protocol.Error {
	return protocol.New(protocol.LoginRequired, "No usable GitHub App private key "+app+": run ensemble-broker login github "+app+".")
}

// writeError never carries Docker's stderr.
func writeError(err error) error {
	if u, ok := errors.AsType[*docker.Unreachable](err); ok {
		return fmt.Errorf("GitHub token injection: %w", u)
	}
	return errors.New("writing a GitHub token into the container failed")
}

// redacted: only a protocol error can hold a secret.
func redacted(err error, secrets ...string) error {
	var e *protocol.Error
	if !errors.As(err, &e) {
		return err
	}
	c := *e
	c.Message, c.Cause = credential.Redact(c.Message, secrets...), credential.Redact(c.Cause, secrets...)
	return &c
}

// detached: a cancelled caller still revokes.
func detached(ctx context.Context) (context.Context, context.CancelFunc) {
	return context.WithTimeout(context.WithoutCancel(ctx), revocationTimeout)
}

func (g GitHub) note(container, result, reason string, m credential.Material, repos *audit.Repositories) {
	credential.Note(g.Audit, audit.GitHubToken, container, result, reason, m, repos)
}

// revoke runs on ctx alone; once it ends, the rest are audited unrevoked. A failure is never retried or returned.
func (g GitHub) revoke(ctx context.Context, container string, tokens []credential.Material) {
	for _, m := range tokens {
		if err := ctx.Err(); err != nil {
			g.note(container, audit.Unrevoked, err.Error(), m, nil)
			continue
		}
		switch err := g.API.Revoke(ctx, m.Token); {
		case err == nil, errors.Is(err, github.ErrAlreadyInvalid):
			g.note(container, audit.Revoked, "", m, nil)
		default:
			g.note(container, audit.Unrevoked, github.Redact(err.Error(), m.Token), m, nil)
		}
	}
}

func (g GitHub) targets(ctx context.Context) (jwt string, _ []github.Installation, _ error) {
	jwt, err := g.jwt(AppKeyService)
	if err != nil {
		return "", nil, err
	}
	installs, err := g.API.Installations(ctx, jwt)
	if err != nil {
		return "", nil, apiError(err)
	}
	return jwt, installs, nil
}

// issue gives no token to an account that is not a GitHub login, so no file name.
func (g GitHub) issue(ctx context.Context, jwt string, in github.Installation, permissions map[string]string, container, result string) (credential.Material, error) {
	if !github.Login.MatchString(in.Account) {
		return credential.Material{}, protocol.New(protocol.GitHubFailed, "GitHub named an installation account that is not a GitHub login.")
	}
	token, expires, err := g.API.Issue(ctx, jwt, in.ID, permissions)
	if err != nil {
		return credential.Material{}, apiError(err)
	}
	m := credential.Material{Account: in.Account, Target: in.ID, Token: token, ExpiresAt: expires.Format(time.RFC3339)}
	g.describe(ctx, container, result, m, jwt)
	return m, nil
}

func (g GitHub) describe(ctx context.Context, container, result string, m credential.Material, jwt string) {
	if g.Audit == nil {
		return
	}
	repos := &audit.Repositories{}
	if selection, names, err := g.API.Repositories(ctx, m.Token); err != nil {
		repos.Error = credential.Redact(github.Redact(err.Error(), m.Token), jwt)
	} else {
		repos.Selection, repos.Names = &selection, names
	}
	g.note(container, result, "", m, repos)
}

func write(exec func([]string, []byte) error, m credential.Material) error {
	if err := credential.Deliver(exec, credential.File{Dir: tokenDir, Name: m.Account, Replace: true}, []byte(m.Token)); err != nil {
		return writeError(err)
	}
	return nil
}

func expiries(tokens []credential.Material) []protocol.Expiry {
	out := make([]protocol.Expiry, 0, len(tokens))
	for _, m := range tokens {
		out = append(out, protocol.Expiry{Account: m.Account, ExpiresAt: m.ExpiresAt})
	}
	slices.SortFunc(out, func(a, b protocol.Expiry) int { return strings.Compare(a.Account, b.Account) })
	return out
}

// Issue: the caller owns delivery and rollback after success.
func (g GitHub) Issue(ctx context.Context, _ string, permissions map[string]string, container string) (commands []credential.Command, revoke func(), _ []protocol.Expiry, err error) {
	jwt, targets, err := g.targets(ctx)
	if err != nil {
		return nil, nil, nil, err
	}
	var issued []credential.Material
	defer func() {
		if err != nil {
			for _, command := range commands {
				command.Erase()
			}
			commands = nil
			rctx, cancel := detached(ctx)
			defer cancel()
			g.revoke(rctx, container, issued)
			err = redacted(err, append(tokens(issued), jwt)...)
		}
	}()
	for _, in := range targets {
		m, issueErr := g.issue(ctx, jwt, in, permissions, container, audit.Issued)
		if issueErr != nil {
			return commands, nil, nil, issueErr
		}
		issued = append(issued, m)
		command, prepErr := credential.Prepare(credential.File{Dir: tokenDir, Name: m.Account, Replace: true}, []byte(m.Token))
		if prepErr != nil {
			return commands, nil, nil, writeError(prepErr)
		}
		commands = append(commands, command)
	}
	var once sync.Once
	return commands, func() {
		once.Do(func() {
			rctx, cancel := context.WithTimeout(context.Background(), revocationTimeout)
			defer cancel()
			g.revoke(rctx, container, issued)
		})
	}, expiries(issued), nil
}

// Refresh never revokes a replaced token: it expires. A new one whose write failed is revoked.
func (g GitHub) Refresh(ctx context.Context, _ string, permissions map[string]string, container string, exec func([]string, []byte) error) ([]protocol.Expiry, error) {
	jwt, targets, err := g.targets(ctx)
	if err != nil {
		return nil, err
	}
	var renewed []credential.Material
	for _, in := range targets {
		m, err := g.issue(ctx, jwt, in, permissions, container, audit.Renewed)
		if err != nil {
			return nil, redacted(err, append(tokens(renewed), jwt)...)
		}
		if err := write(exec, m); err != nil {
			rctx, cancel := detached(ctx)
			defer cancel()
			g.revoke(rctx, container, []credential.Material{m})
			return nil, err
		}
		renewed = append(renewed, m)
	}
	return expiries(renewed), nil
}

// HostLogin erases the token slice once login returns and revokes a failed login's token; a replaced one expires.
func (g GitHub) HostLogin(ctx context.Context, login func(token []byte) error) (protocol.Expiry, error) {
	g.Audit.Renewing("")
	jwt, err := g.jwt(HostAppKeyService)
	if err != nil {
		return protocol.Expiry{}, err
	}
	installs, err := g.API.Installations(ctx, jwt)
	if err != nil {
		return protocol.Expiry{}, redacted(apiError(err), jwt)
	}
	if len(installs) != 1 {
		return protocol.Expiry{}, protocol.New(protocol.GitHubFailed, fmt.Sprintf(
			"GitHub App %s has %d installations; the host App must have exactly one installation.", HostAppKeyService, len(installs)))
	}
	m, err := g.issue(ctx, jwt, installs[0], hostPermissions, HostContainer, audit.Issued)
	if err != nil {
		return protocol.Expiry{}, redacted(err, jwt)
	}
	token := []byte(m.Token)
	err = login(token)
	clear(token)
	if err == nil {
		return protocol.Expiry{Account: m.Account, ExpiresAt: m.ExpiresAt}, nil
	}
	rctx, cancel := detached(ctx)
	defer cancel()
	revoked := "it was revoked"
	switch rerr := g.API.Revoke(rctx, m.Token); {
	case rerr == nil, errors.Is(rerr, github.ErrAlreadyInvalid):
		g.note(HostContainer, audit.Revoked, "", m, nil)
	default:
		g.note(HostContainer, audit.Unrevoked, github.Redact(rerr.Error(), m.Token), m, nil)
		revoked = "it was not revoked and expires at " + m.ExpiresAt
	}
	return protocol.Expiry{}, redacted(protocol.New(protocol.GitHubFailed,
		"Logging the host gh in failed: "+err.Error()+"; "+revoked+"."), m.Token, jwt)
}

func tokens(list []credential.Material) []string {
	out := make([]string, 0, len(list))
	for _, m := range list {
		out = append(out, m.Token)
	}
	return out
}

// Revoke treats the token files as untrusted: they are the container's. A failed read is audited, its tokens left
// to expire. Each audited expiry is the file's mtime, no later than now, plus an hour.
func (g GitHub) Revoke(ctx context.Context, container string, read func(ctx context.Context, argv []string) ([]byte, error)) {
	rctx, cancel := detached(ctx)
	out, err := read(rctx, []string{"/bin/sh", "-c", tokensScript, "sh", tokenDir})
	cancel()
	if docker.Absent(err) {
		return
	}
	var files []credential.Material
	if err == nil {
		files, err = g.tokenFiles(out)
	}
	erase(out)
	if err != nil {
		g.Audit.Add(container, audit.Credential{Kind: audit.GitHubToken, Result: audit.Unrevoked, Reason: "token files unreadable"})
		return
	}
	rctx, cancel = detached(ctx)
	defer cancel()
	g.revoke(rctx, container, files)
}

// tokenFiles errors on more lines than the script lists.
func (g GitHub) tokenFiles(out []byte) ([]credential.Material, error) {
	text := strings.TrimSuffix(string(out), "\n")
	if text == "" {
		return nil, nil
	}
	lines := strings.Split(text, "\n")
	if len(lines) > maxTokenFiles {
		return nil, errors.New("too many token file lines")
	}
	now := g.API.Now().Unix()
	seen := map[string]bool{}
	var files []credential.Material
	for _, line := range lines {
		fields := strings.SplitN(line, " ", 3)
		if len(fields) != 3 || !github.Login.MatchString(fields[0]) || !decimal.MatchString(fields[1]) ||
			!installationToken.MatchString(fields[2]) || seen[fields[2]] {
			continue
		}
		seen[fields[2]] = true
		mtime, _ := strconv.ParseInt(fields[1], 10, 64)
		expires := time.Unix(min(mtime, now), 0).Add(time.Hour).UTC().Format(time.RFC3339)
		files = append(files, credential.Material{Account: fields[0], Token: fields[2], ExpiresAt: expires})
	}
	return files, nil
}

// Prefetch's token reads only repo's contents and is revoked once the prefetch returns.
func (g GitHub) Prefetch(ctx context.Context, repo, ref, dir, container string, run func(argv []string, stdin []byte) error) error {
	jwt, err := g.jwt(AppKeyService)
	if err != nil {
		return err
	}
	in, err := g.API.RepoInstallation(ctx, jwt, repo)
	if err != nil {
		return installError(repo, err)
	}
	token, expires, err := g.API.IssueRepo(ctx, jwt, in.ID, repo, prefetchPermissions)
	if err != nil {
		return apiError(err)
	}
	m := credential.Material{Account: in.Account, Target: in.ID, Token: token, ExpiresAt: expires.Format(time.RFC3339)}
	defer func() {
		rctx, cancel := detached(ctx)
		defer cancel()
		g.revoke(rctx, container, []credential.Material{m})
	}()
	g.describe(ctx, container, audit.Issued, m, jwt)
	if ref == "" {
		if ref, err = g.API.DefaultBranch(ctx, token, repo); err != nil {
			return apiError(err)
		}
		if !launcher.ValidRef(ref) {
			return protocol.Newf(protocol.GitHubFailed, "GitHub named %q the default branch of %s, not a usable branch name.", ref[:min(len(ref), 80)], repo)
		}
	} else {
		kind, err := g.API.RefKind(ctx, token, repo, ref)
		if err != nil {
			return apiError(err)
		}
		if kind == "" {
			return protocol.Newf(protocol.ManifestRejected, "Manifest rejected: prefetch ref %s is no branch or tag of %s.", ref, repo)
		}
	}
	stdin := []byte(token)
	defer erase(stdin)
	if err := run([]string{"/bin/sh", "-c", cloneScript, "sh", repo, ref, dir}, stdin); err != nil {
		if u, ok := errors.AsType[*docker.Unreachable](err); ok {
			return protocol.New(protocol.DockerUnreachable, u.Error())
		}
		failed := protocol.Newf(protocol.StartFailed, "Prefetching %s at %s failed.", repo, ref)
		failed.Cause = github.Redact(docker.Diagnostic(err), token)
		return failed
	}
	return nil
}

func installError(repo string, err error) *protocol.Error {
	var se *github.StatusError
	if errors.As(err, &se) && se.Status == http.StatusNotFound {
		return protocol.New(protocol.GitHubFailed, "GitHub App "+AppKeyService+" is not installed on "+repo+"; install it there or drop prefetch.")
	}
	return apiError(err)
}

func (g GitHub) SaveUser(ctx context.Context, app string, out io.Writer) error {
	rerun := "; rerun ensemble-broker login github " + app
	jwt, err := g.jwt(app)
	if err != nil {
		return err
	}
	slug, err := g.API.AppSlug(ctx, jwt)
	if err != nil {
		return fmt.Errorf("looking up the GitHub App of the %s key failed: %w%s", app, err, rerun)
	}
	login := slug + "[bot]"
	id, err := g.API.BotUser(ctx, login)
	if err != nil {
		return fmt.Errorf("looking up GitHub user %s failed: %w%s", login, err, rerun)
	}
	data, _ := json.Marshal(botUser{slug, id})
	if err := g.Keychain.PutSecret(usersService, app, data); err != nil {
		return fmt.Errorf("saving the bot user ID of %s failed: %w", slug, err)
	}
	_, err = fmt.Fprintf(out, "GitHub App %s bot user %s has ID %d\n", slug, login, id)
	return err
}

// Ready never reads the key.
func (g GitHub) Ready(app string) error {
	if _, err := g.BotUserID(app); err != nil {
		return err
	}
	_, err := g.appID(AppKeyService)
	return err
}

func (g GitHub) BotUserID(app string) (int64, error) {
	rerun := ": run ensemble-broker login github " + AppKeyService + "."
	missing := protocol.New(protocol.LoginRequired, "No GitHub App login"+rerun)
	data, err := g.Keychain.Secret(usersService, AppKeyService)
	switch {
	case errors.Is(err, ErrNotFound):
		return 0, missing
	case err != nil:
		return 0, protocol.New(protocol.KeychainFailed, "Keychain access to the GitHub bot user ID failed"+retrustRemedy)
	}
	var user botUser
	if json.Unmarshal(data, &user) != nil || !github.Login.MatchString(user.Slug) || user.ID < 1 {
		return 0, missing
	}
	if app != user.Slug {
		return 0, protocol.New(protocol.ManifestRejected, "Manifest rejected: github app must be "+user.Slug+", the App login github saved.")
	}
	return user.ID, nil
}

func (g GitHub) appID(app string) (string, error) {
	account, err := g.Apps.AppAccount(app)
	switch {
	case errors.Is(err, ErrNotFound), errors.Is(err, ErrAmbiguous), err == nil && !appID.MatchString(account):
		return "", loginError(app)
	case err != nil:
		return "", protocol.New(protocol.KeychainFailed, "Keychain access to the GitHub App private key failed.")
	}
	return account, nil
}

func (g GitHub) jwt(app string) (string, error) {
	id, err := g.appID(app)
	if err != nil {
		return "", err
	}
	data, err := g.Apps.AppKey(app, id)
	defer erase(data)
	switch {
	case errors.Is(err, ErrNotFound):
		return "", loginError(app)
	case errors.Is(err, ErrNeedsDialog):
		return "", protocol.New(protocol.KeychainFailed, "The GitHub App private key "+app+" needs keychain approval"+retrustRemedy)
	case err != nil:
		return "", protocol.New(protocol.KeychainFailed, "Keychain access to the GitHub App private key failed.")
	}
	key, err := github.ParseKey(data)
	if err != nil {
		return "", protocol.New(protocol.GitHubFailed, "GitHub App private key "+app+" unusable: "+err.Error()+".")
	}
	jwt, err := g.API.JWT(id, key)
	if err != nil {
		return "", protocol.New(protocol.GitHubFailed, "Signing the GitHub App token failed.")
	}
	return jwt, nil
}
