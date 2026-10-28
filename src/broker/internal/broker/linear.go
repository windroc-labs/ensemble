package broker

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"time"

	"ensemble/broker/internal/audit"
	"ensemble/broker/internal/credential"
	"ensemble/broker/internal/protocol"
)

const (
	linearService       = "ensemble-broker-linear"
	linearGrantsService = "ensemble-broker-linear-grants"
	linearTokenURL      = "https://api.linear.app/oauth/token"
)

var linearName = regexp.MustCompile(`^[A-Za-z0-9._@+-]{1,128}$`)
var linearUser = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)
var errLinear = errors.New("Linear broker maintenance failed; check enrollment and broker installation, then rerun")

// ErrLinearStale is linear-maintain meeting a broker Linear item an earlier broker build wrote, which retrust repairs.
var ErrLinearStale = errors.New("a broker Linear keychain item trusts an earlier broker build: run " + RetrustCommand)

// LinearIdentity is public enrollment metadata, never a credential. Kind fixes the requested scopes.
type LinearIdentity struct {
	Name          string `json:"name"`
	Kind          string `json:"kind"`
	ClientID      string `json:"client_id"`
	UserID        string `json:"user_id"`
	SecretService string `json:"secret_service"`
	TokenService  string `json:"token_service"`
}

func (a LinearIdentity) scopes() string {
	switch a.Kind {
	case "harness":
		return "read,write"
	case "manager":
		return "read,write,app:assignable"
	case "role":
		return "read,write,app:assignable,app:mentionable"
	}
	return ""
}

func linearSafe(s string) bool { return linearName.MatchString(s) && s != "." && s != ".." }

func (a LinearIdentity) check() error {
	if !linearSafe(a.Name) || !linearSafe(a.SecretService) || !linearSafe(a.TokenService) || a.SecretService == a.TokenService || a.scopes() == "" || !linearUser.MatchString(a.UserID) || len(a.ClientID) == 0 || len(a.ClientID) > 256 || strings.ContainsFunc(a.ClientID, func(r rune) bool { return r <= 0x20 || r > 0x7e }) {
		return errLinear
	}
	for _, service := range []string{a.SecretService, a.TokenService} {
		for _, reserved := range KeychainServices {
			if service == reserved || (strings.HasSuffix(reserved, "/") && strings.HasPrefix(service, reserved)) {
				return errLinear
			}
		}
	}
	return nil
}

// ReadLinearIdentity accepts exactly one bounded object, rejecting duplicate/unknown fields before credential access.
func ReadLinearIdentity(r io.Reader) (LinearIdentity, error) {
	var a LinearIdentity
	data, err := io.ReadAll(io.LimitReader(r, 8193))
	if err != nil || len(data) > 8192 {
		return a, errLinear
	}
	d := json.NewDecoder(bytes.NewReader(data))
	first, err := d.Token()
	if err != nil || first != json.Delim('{') {
		return a, errLinear
	}
	seen := map[string]bool{}
	for d.More() {
		key, err := d.Token()
		if err != nil {
			return a, errLinear
		}
		name, ok := key.(string)
		if !ok || seen[name] {
			return a, errLinear
		}
		switch name {
		case "name", "kind", "client_id", "user_id", "secret_service", "token_service":
		default:
			return a, errLinear
		}
		seen[name] = true
		var v json.RawMessage
		if d.Decode(&v) != nil {
			return a, errLinear
		}
	}
	if _, err = d.Token(); err != nil {
		return a, errLinear
	}
	var extra any
	if d.Decode(&extra) != io.EOF {
		return a, errLinear
	}
	d = json.NewDecoder(bytes.NewReader(data))
	d.DisallowUnknownFields()
	if d.Decode(&a) != nil || a.check() != nil {
		return LinearIdentity{}, errLinear
	}
	return a, nil
}

type linearKeys interface {
	SecretKeychain
	ItemTrust
	AppAccount(string) (string, error)
	UpdateSecret(string, string, []byte) error
}

type linearHost interface {
	Read(service, account string) ([]byte, error)
	Publish(context.Context, string, string, []byte) error
}

type linearAccess struct {
	AccessToken string `json:"access_token"`
	ExpiresAt   string `json:"expires_at"`
}

type linearAccount struct {
	Version      int            `json:"version"`
	Identity     LinearIdentity `json:"identity"`
	RootAccount  string         `json:"root_account"`
	TokenAccount string         `json:"token_account"`
	Active       bool           `json:"active"`
	Current      linearAccess   `json:"current"`
	RenewAt      string         `json:"renew_at"`
}

// Linear owns enrolled accounts, host grants and per-container revocation records; the launcher delivers its commands.
type Linear struct {
	Audit    *audit.Recorder
	keys     linearKeys
	host     linearHost
	http     *http.Client
	lock     func(string) func() (func(), error)
	now      func() time.Time
	password func() ([]byte, error)
}

// NewLinear is Linear for the account at home, its items in k.
func NewLinear(home string, k SystemKeychain, password func() ([]byte, error)) Linear {
	return Linear{keys: k, host: linearHostEntries{home: home, run: runLinearSecurity, access: k.AppAccess},
		http: &http.Client{Timeout: 30 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}, now: time.Now, password: password,
		lock: func(id string) func() (func(), error) {
			return FileLock(filepath.Join(home, ".ensemble", "linear", fmt.Sprintf("%x.lock", sha256.Sum256([]byte(id)))))
		}}
}

func (l Linear) read(account string, into any) error {
	b, err := (linearStore{l.keys}).Secret(linearService, account)
	defer erase(b)
	if err != nil {
		return err
	}
	if len(b) > 1<<20 || json.Unmarshal(b, into) != nil {
		return errLinear
	}
	return nil
}

func (l Linear) save(account string, value any) error {
	b, err := json.Marshal(value)
	defer erase(b)
	if err != nil || (linearStore{l.keys}).PutSecret(linearService, account, b) != nil {
		return errLinear
	}
	return nil
}

// Enroll reserves a binding before changing its root ACL. A retry completes pending work; rebinding is refused.
func (l Linear) Enroll(a LinearIdentity) error {
	if a.check() != nil {
		return errLinear
	}
	unlock, err := l.lock("account/" + a.TokenService)()
	if err != nil {
		return errLinear
	}
	defer unlock()
	passwordSource := l.password
	var password []byte
	var passwordErr error
	asked := false
	l.password = func() ([]byte, error) {
		if !asked {
			asked = true
			if passwordSource == nil {
				passwordErr = errLinear
			} else {
				password, passwordErr = passwordSource()
			}
		}
		return bytes.Clone(password), passwordErr
	}
	defer func() { erase(password) }()
	for _, item := range [][2]string{{linearService, "registry"}, {linearService, "app/" + a.TokenService}, {linearGrantsService, "host/" + a.TokenService}} {
		if l.repairMetadata(item[0], item[1]) != nil {
			return errLinear
		}
	}
	account, err := l.reserve(a)
	if err != nil {
		return err
	}
	if secureItem(a.SecretService, account.RootAccount, "rerun login linear", l.keys, l.password) != nil {
		return errLinear
	}
	trusted, err := trustedItem(l.keys, a.SecretService, account.RootAccount)
	if err != nil || !trusted {
		return errLinear
	}
	account.Active = true
	return l.save("app/"+a.TokenService, account)
}

// reserve is a short registry transaction. It never waits for another account, calls a provider or prompts.
func (l Linear) reserve(a LinearIdentity) (linearAccount, error) {
	unlock, err := l.lock("registry")()
	if err != nil {
		return linearAccount{}, errLinear
	}
	defer unlock()
	var registry []LinearIdentity
	err = l.read("registry", &registry)
	if err != nil && !errors.Is(err, ErrNotFound) {
		return linearAccount{}, errLinear
	}
	exists := false
	for _, old := range registry {
		if old == a {
			exists = true
			continue
		}
		if old.ClientID == a.ClientID || old.UserID == a.UserID || old.Name == a.Name || slices.Contains([]string{old.SecretService, old.TokenService}, a.SecretService) || slices.Contains([]string{old.SecretService, old.TokenService}, a.TokenService) {
			return linearAccount{}, errLinear
		}
	}
	var account linearAccount
	err = l.read("app/"+a.TokenService, &account)
	newAccount := errors.Is(err, ErrNotFound)
	if errors.Is(err, ErrNotFound) {
		root, err := l.keys.AppAccount(a.SecretService)
		if err != nil || !linearSafe(root) {
			return linearAccount{}, errLinear
		}
		token, err := l.keys.AppAccount(a.TokenService)
		if errors.Is(err, ErrNotFound) {
			token, err = a.TokenService, nil
		}
		if err != nil || !linearSafe(token) {
			return linearAccount{}, errLinear
		}
		account = linearAccount{Version: 1, Identity: a, RootAccount: root, TokenAccount: token}
	} else if err != nil || account.Version != 1 || account.Identity != a {
		return linearAccount{}, errLinear
	}
	if !exists {
		if l.save("registry", append(registry, a)) != nil {
			return linearAccount{}, errLinear
		}
	}
	if newAccount {
		if l.save("app/"+a.TokenService, account) != nil {
			return linearAccount{}, errLinear
		}
	}
	return account, nil
}

// LinearResult is the complete non-secret host maintenance reply. It is separate from the Docker protocol.
type LinearResult struct {
	Version   int    `json:"version"`
	Renewed   bool   `json:"renewed"`
	ExpiresAt string `json:"expires_at"`
}

func (l Linear) Maintain(ctx context.Context, a LinearIdentity) (LinearResult, error) {
	if a.check() != nil {
		return LinearResult{}, errLinear
	}
	unlock, err := l.lock("account/" + a.TokenService)()
	if err != nil {
		return LinearResult{}, errLinear
	}
	defer unlock()
	for _, item := range [][2]string{{linearService, "registry"}, {linearService, "app/" + a.TokenService}, {linearGrantsService, "host/" + a.TokenService}} {
		if _, earlier, err := checkItem(l.keys, item[0], item[1]); err == nil && earlier {
			return LinearResult{}, ErrLinearStale
		}
	}
	var registry []LinearIdentity
	if l.read("registry", &registry) != nil || !slices.Contains(registry, a) {
		return LinearResult{}, errLinear
	}
	var account linearAccount
	if l.read("app/"+a.TokenService, &account) != nil || account.Version != 1 || !account.Active || account.Identity != a {
		return LinearResult{}, errLinear
	}
	trusted, err := trustedItem(l.keys, a.SecretService, account.RootAccount)
	if err != nil || !trusted {
		return LinearResult{}, errLinear
	}
	p := &linearProvider{owner: l, account: &account}
	lifecycle := credential.Lifecycle{Provider: p, Store: linearStore{l.keys}, NotFound: ErrNotFound, Records: linearGrantsService, Lock: l.lock("grant/" + a.TokenService), Audit: l.Audit}
	d := credential.Delivery{Consumer: credential.Consumer{Host: a.TokenService}, Publish: func(m credential.Material) error {
		value, _ := json.Marshal(linearAccess{AccessToken: m.Token, ExpiresAt: m.ExpiresAt})
		defer erase(value)
		return l.host.Publish(ctx, a.TokenService, account.TokenAccount, value)
	}}
	scope := map[string]string{"scope": a.scopes()}
	_, err = lifecycle.Refresh(ctx, d, scope)
	if errors.Is(err, credential.ErrNoRecord) {
		_, err = lifecycle.Acquire(ctx, d, a.TokenService, scope)
	}
	if err != nil {
		return LinearResult{}, errLinear
	}
	return LinearResult{Version: 1, Renewed: p.renewed, ExpiresAt: account.Current.ExpiresAt}, nil
}

type linearProvider struct {
	owner   Linear
	account *linearAccount
	renewed bool
}

func (*linearProvider) Kind() credential.Kind { return credential.Linear }
func (*linearProvider) AuditKind() string     { return audit.LinearAccessToken }
func (p *linearProvider) Begin(_ context.Context, subject string) (credential.Issuer, error) {
	if subject != p.account.Identity.TokenService {
		return nil, errLinear
	}
	return p, nil
}
func (p *linearProvider) Secret() string { return "" }
func (p *linearProvider) Targets(context.Context) ([]credential.Material, error) {
	return []credential.Material{{Account: p.account.Identity.Name}}, nil
}

func linearClean(s string) bool {
	return len(s) > 0 && len(s) <= maxToken && !strings.ContainsFunc(s, func(r rune) bool { return r <= 0x20 || r > 0x7e })
}

func (p *linearProvider) Issue(ctx context.Context, scope map[string]string) (credential.Material, error) {
	a, l := p.account, p.owner
	if scope["scope"] != a.Identity.scopes() {
		return credential.Material{}, errLinear
	}
	if a.Current.AccessToken == "" {
		data, _ := l.host.Read(a.Identity.TokenService, a.TokenAccount)
		_ = json.Unmarshal(data, &a.Current)
		erase(data)
		if expires, err := time.Parse(time.RFC3339, a.Current.ExpiresAt); err == nil {
			a.Current.ExpiresAt = expires.UTC().Format(time.RFC3339Nano)
			a.RenewAt = expires.Add(-5 * 24 * time.Hour).Format(time.RFC3339)
		}
	}
	now := l.now()
	renewAt, _ := time.Parse(time.RFC3339, a.RenewAt)
	expires, _ := time.Parse(time.RFC3339, a.Current.ExpiresAt)
	if !expires.IsZero() {
		a.Current.ExpiresAt = expires.UTC().Format(time.RFC3339Nano)
	}
	if !linearClean(a.Current.AccessToken) || now.After(renewAt) || !now.Before(expires) {
		l.Audit.Renewing("")
		secret, err := l.keys.Secret(a.Identity.SecretService, a.RootAccount)
		if err != nil {
			erase(secret)
			return credential.Material{}, errLinear
		}
		defer erase(secret)
		if !linearClean(string(secret)) {
			return credential.Material{}, errLinear
		}
		current, seconds, err := l.request(ctx, a.Identity, string(secret))
		if err != nil {
			l.Audit.Renewing(protocol.RefreshFailed)
			return credential.Material{}, errLinear
		}
		a.Current = current
		a.RenewAt = now.Add(max(seconds/2, seconds-5*24*time.Hour)).Format(time.RFC3339Nano)
		p.renewed = true
	}
	if l.save("app/"+a.Identity.TokenService, a) != nil {
		return credential.Material{}, errLinear
	}
	return credential.Material{Account: a.Identity.Name, Token: a.Current.AccessToken, ExpiresAt: a.Current.ExpiresAt}, nil
}

func (l Linear) request(ctx context.Context, a LinearIdentity, secret string) (linearAccess, time.Duration, error) {
	form := url.Values{"grant_type": {"client_credentials"}, "client_id": {a.ClientID}, "client_secret": {secret}, "scope": {a.scopes()}}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, linearTokenURL, strings.NewReader(form.Encode()))
	if err != nil {
		return linearAccess{}, 0, errLinear
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	resp, err := l.http.Do(req)
	if err != nil {
		return linearAccess{}, 0, errLinear
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return linearAccess{}, 0, errLinear
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, 65537))
	defer erase(data)
	var result struct {
		Token   string `json:"access_token"`
		Seconds int64  `json:"expires_in"`
	}
	if err != nil || len(data) > 65536 || json.Unmarshal(data, &result) != nil || !linearClean(result.Token) || result.Token == secret || result.Seconds <= 0 || result.Seconds > int64((365*24*time.Hour)/time.Second) {
		return linearAccess{}, 0, errLinear
	}
	lifetime := time.Duration(result.Seconds) * time.Second
	return linearAccess{AccessToken: result.Token, ExpiresAt: l.now().Add(lifetime).UTC().Format(time.RFC3339Nano)}, lifetime, nil
}
