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
	"syscall"
	"time"

	"ensemble/broker/internal/audit"
	"ensemble/broker/internal/credential"
	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/github"
	"ensemble/broker/internal/launcher"
	"ensemble/broker/internal/protocol"
)

// The broker-only items: a token record per container, one revocation backlog, and each App's bot user ID.
const (
	usersService       = "ensemble-broker-github-users"
	tokensService      = "ensemble-broker-tokens"
	revocationsService = "ensemble-broker-revocations"
	revocationsAccount = "pending"
	tokenDir           = docker.SecretsDir + "/github"
)

// cloneScript shallow-clones branch or tag $2 of github.com/$1 into $3 with the prefetch token on stdin, kept only in
// $HOME (the helper's tmpfs). git reads it through a command-line credential helper, so the clone's config and remote
// URL never hold it, and runs none of the repo's hooks.
const cloneScript = `set -eu
umask 077
cat > "$HOME/token"
exec git -c core.hooksPath=/dev/null -c core.fsmonitor=false -c credential.helper= ` +
	`-c 'credential.helper=!f() { test "$1" = get || exit 0; echo username=x-access-token; printf "password=%s\n" "$(cat "$HOME/token")"; }; f' ` +
	`clone --quiet --depth 1 --branch "$2" -- "https://github.com/$1.git" "$3"`

// prefetchPermissions is all a prefetch token may do: read its one repository's contents.
var prefetchPermissions = map[string]string{"contents": "read"}

// botUser is what login github keeps in the users item: the App's slug and its bot user's ID.
type botUser struct {
	Slug string `json:"slug"`
	ID   int64  `json:"id"`
}

// AppKeys reads a GitHub App's private-key item.
type AppKeys interface {
	AppAccount(app string) (string, error)
	AppKey(app, account string) ([]byte, error)
}

// GitHub is the GitHub provider on the token lifecycle (credential.Lifecycle): each container's grant is one
// installation token per installation, in the container's record or the backlog until revoked or expired;
// revocation uses only those values, never the container's files. Lock guards each read-modify-write of either
// item, never a GitHub request. Audit (nil: none) gets each token's outcome, never its value.
type GitHub struct {
	Keychain SecretKeychain
	Apps     AppKeys
	API      github.Client
	Lock     func() (unlock func(), err error)
	Audit    *audit.Recorder
}

// FileLock is an exclusive flock on path, created 0600 in a 0700 directory.
func FileLock(path string) func() (func(), error) {
	return func() (func(), error) {
		if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
			return nil, err
		}
		f, err := os.OpenFile(path, os.O_RDWR|os.O_CREATE, 0o600)
		if err != nil {
			return nil, err
		}
		if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX); err != nil {
			f.Close()
			return nil, err
		}
		return func() { f.Close() }, nil
	}
}

func unrevoked(container string) *protocol.Error {
	return protocol.New(protocol.KeychainFailed, "The GitHub tokens of "+container+" could not be revoked; they expire within an hour.")
}

func apiError(err error) *protocol.Error {
	return protocol.New(protocol.GitHubFailed, "GitHub request failed: "+err.Error()+".")
}

func loginError(app string) *protocol.Error {
	return protocol.New(protocol.LoginRequired, "No usable GitHub App private key "+app+": run ensemble-broker login github "+AppKeyService+".")
}

// tokensError is the protocol's account of a lifecycle error about container's tokens.
func tokensError(container string, err error) error {
	var e *protocol.Error
	switch {
	case err == nil:
		return nil
	case errors.As(err, new(*credential.UnrevokedError)):
		return unrevoked(container)
	case errors.As(err, &e):
		return e
	case errors.Is(err, credential.ErrLock):
		return protocol.New(protocol.KeychainFailed, "Taking the GitHub token lock ~/.ensemble/docker/tokens.lock failed.")
	case errors.Is(err, credential.ErrStore):
		return protocol.New(protocol.KeychainFailed, "Keychain access to the GitHub tokens failed.")
	case errors.Is(err, credential.ErrNoRecord):
		return protocol.New(protocol.RefreshFailed, "No GitHub tokens recorded for "+container+".")
	case errors.Is(err, credential.ErrReleased):
		return protocol.New(protocol.RefreshFailed, container+" was closed during the refresh.")
	case errors.Is(err, docker.ErrUnreachable):
		return fmt.Errorf("GitHub token injection: %w", docker.ErrUnreachable)
	case errors.Is(err, credential.ErrDelivery):
		return errors.New("writing a GitHub token into the container failed")
	}
	return err
}

// lifecycle is g's token lifecycle over the ensemble-broker-tokens records and the ensemble-broker-revocations backlog.
func (g GitHub) lifecycle() credential.Lifecycle {
	return credential.Lifecycle{
		Provider: githubProvider{g},
		Store:    g.Keychain,
		NotFound: ErrNotFound,
		Records:  tokensService,
		Backlog:  [2]string{revocationsService, revocationsAccount},
		Lock:     g.Lock,
		Audit:    g.Audit,
	}
}

func to(container string, exec func([]string, []byte) error) credential.Delivery {
	return credential.Delivery{Consumer: credential.Consumer{Container: container}, Exec: exec}
}

// Issue gives container one token per installation of app, limited to permissions, recording each before the next
// request and writing them in; on any failure it revokes every token it issued.
func (g GitHub) Issue(ctx context.Context, app string, permissions map[string]string, container string, exec func([]string, []byte) error) ([]protocol.Expiry, error) {
	expiries, err := g.lifecycle().Acquire(ctx, to(container, exec), app, permissions)
	return expiries, tokensError(container, err)
}

// Refresh replaces each of container's tokens with one limited to its recorded permissions (permissions for a record
// an older broker wrote): a new one is recorded and written in before the old is revoked.
func (g GitHub) Refresh(ctx context.Context, _ string, permissions map[string]string, container string, exec func([]string, []byte) error) ([]protocol.Expiry, error) {
	expiries, err := g.lifecycle().Refresh(ctx, to(container, exec), permissions)
	return expiries, tokensError(container, err)
}

// Prefetch clones ref ("": the default branch) of repo into dir through run, which starts the helper container with
// argv and stdin. The token it gives run reads only repo's contents; the lifecycle holds it in container's record while
// in use, so closing container retires it should the broker die, and it is revoked once run returns.
func (g GitHub) Prefetch(ctx context.Context, repo, ref, dir, container string, run func(argv []string, stdin []byte) error) error {
	jwt, err := g.jwt(AppKeyService)
	if err != nil {
		return err
	}
	in, err := g.API.RepoInstallation(ctx, jwt, repo)
	var se *github.StatusError
	if errors.As(err, &se) && se.Status == http.StatusNotFound {
		return protocol.New(protocol.GitHubFailed, "GitHub App "+AppKeyService+" is not installed on "+repo+"; install it there or drop prefetch.")
	}
	if err != nil {
		return apiError(err)
	}
	token, expires, err := g.API.IssueRepo(ctx, jwt, in.ID, repo, prefetchPermissions)
	if err != nil {
		return apiError(err)
	}
	lc, c := g.lifecycle(), credential.Consumer{Container: container}
	m := credential.Material{Account: in.Account, Target: in.ID, Token: token, ExpiresAt: expires.Format(time.RFC3339)}
	if err := lc.Hold(ctx, c, m); err != nil {
		return tokensError(container, err)
	}
	defer func() {
		rctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 30*time.Second) // a cancelled start still revokes it
		defer cancel()
		lc.Retire(rctx, c, m)
	}()
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
		if errors.Is(err, docker.ErrUnreachable) {
			return protocol.New(protocol.DockerUnreachable, "Cannot reach Docker Desktop; start it and retry.")
		}
		failed := protocol.Newf(protocol.StartFailed, "Prefetching %s at %s failed.", repo, ref)
		failed.Cause = github.Redact(docker.Diagnostic(err), token)
		return failed
	}
	return nil
}

// Release retires every token of container's record, its grant; an unreadable record is kept for recovery.
func (g GitHub) Release(ctx context.Context, container string) error {
	return tokensError(container, g.lifecycle().Release(ctx, credential.Consumer{Container: container}))
}

// Pending projects a single Keychain snapshot without locking, mutation, remote requests or audit.
func (g GitHub) Pending() ([]protocol.Pending, error) {
	pending, err := g.lifecycle().Pending()
	return pending, tokensError("", err)
}

// Retry revokes the backlog: expired entries are dropped unrevoked, and the first request that gets no answer
// stops the run, keeping the rest for next time.
func (g GitHub) Retry(ctx context.Context) ([]protocol.Pending, error) {
	pending, err := g.lifecycle().Retry(ctx)
	return pending, tokensError("", err)
}

// githubProvider is GitHub's side of the lifecycle: installations, the App JWT and the token API.
type githubProvider struct{ g GitHub }

func (p githubProvider) Kind() credential.Kind { return credential.GitHub }
func (p githubProvider) AuditKind() string     { return audit.GitHubToken }
func (p githubProvider) Now() time.Time        { return p.g.API.Now() }

// Begin signs the App JWT with the AppKeyService key; its errors are protocol errors without the key or JWT.
func (p githubProvider) Begin(_ context.Context, _ string) (credential.Issuer, error) {
	jwt, err := p.g.jwt(AppKeyService)
	if err != nil {
		return nil, err
	}
	return githubIssuer{p.g.API, jwt}, nil
}

// File is m's token file, refusing an account that is not a GitHub login before anything is written.
func (p githubProvider) File(m credential.Material) (credential.File, error) {
	if !github.Login.MatchString(m.Account) {
		return credential.File{}, fmt.Errorf("account %q is not a GitHub login", m.Account)
	}
	return credential.File{Dir: tokenDir, Name: m.Account, Replace: true}, nil
}

func (p githubProvider) Revoke(ctx context.Context, m credential.Material) credential.Revocation {
	err := p.g.API.Revoke(ctx, m.Token)
	switch {
	case err == nil:
		return credential.Revocation{Outcome: credential.Revoked}
	case errors.Is(err, github.ErrAlreadyInvalid):
		return credential.Revocation{Outcome: credential.AlreadyInvalid}
	}
	return credential.Revocation{Outcome: credential.Retryable, Err: err, Unreachable: github.Transport(err)}
}

// Describe is the repositories m reaches; a failed listing is described, never returned.
func (p githubProvider) Describe(ctx context.Context, m credential.Material) *audit.Repositories {
	repos := &audit.Repositories{}
	if selection, names, err := p.g.API.Repositories(ctx, m.Token); err != nil {
		repos.Error = err.Error()
	} else {
		repos.Selection, repos.Names = &selection, names
	}
	return repos
}

type githubIssuer struct {
	api github.Client
	jwt string
}

func (i githubIssuer) Secret() string { return i.jwt }

func (i githubIssuer) Targets(ctx context.Context) ([]credential.Material, error) {
	installs, err := i.api.Installations(ctx, i.jwt)
	if err != nil {
		return nil, apiError(err)
	}
	targets := make([]credential.Material, 0, len(installs))
	for _, in := range installs {
		targets = append(targets, credential.Material{Account: in.Account, Target: in.ID})
	}
	return targets, nil
}

func (i githubIssuer) Issue(ctx context.Context, scope map[string]string, target credential.Material) (credential.Material, error) {
	token, expires, err := i.api.Issue(ctx, i.jwt, target.Target, scope)
	if err != nil {
		return credential.Material{}, apiError(err)
	}
	return credential.Material{Token: token, ExpiresAt: expires.Format(time.RFC3339)}, nil
}

// SaveUser asks GitHub which App the AppKeyService key is (its slug), looks up its bot user (`<slug>[bot]`) and keeps
// both, for start to check and report; out gets its one line.
func (g GitHub) SaveUser(ctx context.Context, out io.Writer) error {
	rerun := "; rerun ensemble-broker login github " + AppKeyService
	jwt, err := g.jwt(AppKeyService)
	if err != nil {
		return err
	}
	slug, err := g.API.AppSlug(ctx, jwt)
	if err != nil {
		return fmt.Errorf("looking up the GitHub App of the %s key failed: %w%s", AppKeyService, err, rerun)
	}
	login := slug + "[bot]"
	id, err := g.API.BotUser(ctx, login)
	if err != nil {
		return fmt.Errorf("looking up GitHub user %s failed: %w%s", login, err, rerun)
	}
	data, _ := json.Marshal(botUser{slug, id})
	if err := g.Keychain.PutSecret(usersService, AppKeyService, data); err != nil {
		return fmt.Errorf("saving the bot user ID of %s failed: %w", slug, err)
	}
	_, err = fmt.Fprintf(out, "GitHub App %s bot user %s has ID %d\n", slug, login, id)
	return err
}

// Ready checks, without reading the key, that app is the App login github saved and its key item is in place.
func (g GitHub) Ready(app string) error {
	if _, err := g.BotUserID(app); err != nil {
		return err
	}
	_, err := g.appID(AppKeyService)
	return err
}

// BotUserID is app's bot user ID as login github saved it; another app than the saved one is refused.
func (g GitHub) BotUserID(app string) (int64, error) {
	rerun := ": run ensemble-broker login github " + AppKeyService + "."
	missing := protocol.New(protocol.LoginRequired, "No GitHub App login"+rerun)
	data, err := g.Keychain.Secret(usersService, AppKeyService)
	switch {
	case errors.Is(err, ErrNotFound):
		return 0, missing
	case err != nil:
		return 0, protocol.New(protocol.KeychainFailed, "Keychain access to the GitHub bot user ID failed"+rerun)
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
