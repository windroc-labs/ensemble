package broker

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/url"
	"os"
	"slices"
	"strings"
	"time"

	"ensemble/broker/internal/audit"
	"ensemble/broker/internal/credential"
	"ensemble/broker/internal/protocol"
)

// linearContainerRecord keeps revocation possible after the guard and tmpfs stop.
// Closed blocks queued maintenance; Tokens retains failed revocations beside Current.
type linearContainerRecord struct {
	Closed  bool           `json:"closed"`
	Current linearAccess   `json:"current"`
	Tokens  []linearAccess `json:"tokens"`
}

func roleIdentity(a credential.LinearApp) LinearIdentity {
	return LinearIdentity{Name: a.Role, Kind: "role", ClientID: a.ClientID, UserID: a.UserID, SecretService: a.SecretService, TokenService: a.TokenService}
}

// containerAccount accepts only the exact active role binding enrolled on the host.
func (l Linear) containerAccount(app credential.LinearApp) (linearAccount, error) {
	a := roleIdentity(app)
	var registry []LinearIdentity
	var account linearAccount
	if l.read("registry", &registry) != nil || !slices.Contains(registry, a) || l.read("app/"+a.TokenService, &account) != nil || account.Version != 1 || !account.Active || account.Identity != a {
		return linearAccount{}, errLinear
	}
	trusted, err := trustedItem(l.keys, a.SecretService, account.RootAccount)
	if err != nil || !trusted {
		return linearAccount{}, errLinear
	}
	return account, nil
}

func (l Linear) Ready(app credential.LinearApp) error {
	if _, err := l.containerAccount(app); err != nil {
		return protocol.New(protocol.LoginRequired, "Linear role app is not enrolled or readable; run ensemble-broker login linear on the host.")
	}
	return nil
}

// containerToken issues separately from the host's shared token and never publishes it there.
func (l Linear) containerToken(ctx context.Context, app credential.LinearApp) (linearAccess, error) {
	account, err := l.containerAccount(app)
	if err != nil {
		return linearAccess{}, errLinear
	}
	secret, err := l.keys.Secret(app.SecretService, account.RootAccount)
	defer erase(secret)
	if err != nil || !linearClean(string(secret)) {
		return linearAccess{}, errLinear
	}
	token, _, err := l.request(ctx, account.Identity, string(secret))
	return token, err
}

func linearCommand(token linearAccess, replace bool) credential.Command {
	data, _ := json.Marshal(token)
	defer erase(data)
	command, _ := credential.Prepare(credential.File{Dir: credential.LinearDir, Name: "token.json", Replace: replace}, data)
	return command
}

func (l Linear) noteContainer(c, result, reason string, token linearAccess) {
	l.Audit.Add(c, audit.Credential{Kind: audit.LinearAccessToken, Result: result, Reason: reason, ExpiresAt: &token.ExpiresAt})
}

func (l Linear) revokeContainerToken(ctx context.Context, c string, token linearAccess) bool {
	if expiry, err := time.Parse(time.RFC3339, token.ExpiresAt); err == nil && !l.now().Before(expiry) {
		l.noteContainer(c, audit.Withdrawn, "", token)
		return true
	}
	rctx, cancel := detached(ctx)
	defer cancel()
	form := url.Values{"token": {token.AccessToken}}
	req, err := http.NewRequestWithContext(rctx, http.MethodPost, "https://api.linear.app/oauth/revoke", strings.NewReader(form.Encode()))
	result, reason := audit.Unrevoked, "token revocation failed"
	if err == nil {
		req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
		resp, err := l.http.Do(req)
		if err == nil {
			_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 65536))
			_ = resp.Body.Close()
			if resp.StatusCode == http.StatusOK || resp.StatusCode == http.StatusUnauthorized {
				result, reason = audit.Revoked, ""
			}
		}
	}
	l.noteContainer(c, result, reason, token)
	return result == audit.Revoked
}

// retireContainer keeps every failed revocation and the active token, if any.
func (l Linear) retireContainer(ctx context.Context, c string, record *linearContainerRecord, keep string) error {
	pending := make([]linearAccess, 0, len(record.Tokens))
	failed := false
	for _, token := range record.Tokens {
		if token.AccessToken == keep {
			pending = append(pending, token)
		} else if !l.revokeContainerToken(ctx, c, token) {
			pending = append(pending, token)
			failed = true
		}
	}
	record.Tokens = pending
	var err error
	if len(pending) == 0 {
		err = l.keys.DeleteSecret(linearService, "container/"+c)
	} else {
		err = l.save("container/"+c, record)
	}
	if err != nil || failed {
		return errLinear
	}
	return nil
}

func (l Linear) Issue(ctx context.Context, r credential.LinearRequest, c string) ([]credential.Command, func(), string, error) {
	unlock, err := l.lock("container/"+c, true)()
	if err != nil {
		return nil, nil, "", errLinear
	}
	defer unlock()
	var existing linearContainerRecord
	if err := l.read("container/"+c, &existing); !errors.Is(err, ErrNotFound) {
		return nil, nil, "", errLinear
	}
	token, err := l.containerToken(ctx, r.App)
	if err != nil {
		return nil, nil, "", errLinear
	}
	l.noteContainer(c, audit.Issued, "", token)
	record := linearContainerRecord{Current: token, Tokens: []linearAccess{token}}
	if l.save("container/"+c, record) != nil {
		l.revokeContainerToken(ctx, c, token)
		return nil, nil, "", errLinear
	}
	command := linearCommand(token, false)
	board, _ := json.Marshal(r.Board)
	defer erase(board)
	config, _ := credential.Prepare(credential.File{Dir: credential.LinearDir, Name: "board.json"}, board)
	return []credential.Command{command, config}, func() { _ = l.Revoke(ctx, c) }, token.ExpiresAt, nil
}

func (l Linear) Refresh(ctx context.Context, r credential.LinearRequest, c string, exec func([]string, []byte) error) (string, error) {
	unlock, err := l.lock("container/"+c, true)()
	if err != nil {
		return "", errLinear
	}
	defer unlock()
	var record linearContainerRecord
	if l.read("container/"+c, &record) != nil || record.Closed {
		return "", errLinear
	}
	token, err := l.containerToken(ctx, r.App)
	if err != nil {
		return "", errLinear
	}
	l.noteContainer(c, audit.Renewed, "", token)
	record.Tokens = append(record.Tokens, token)
	if l.save("container/"+c, record) != nil {
		l.revokeContainerToken(ctx, c, token)
		return "", errLinear
	}
	command := linearCommand(token, true)
	defer command.Erase()
	if err := exec(command.Argv, command.Stdin); err != nil {
		_ = l.retireContainer(ctx, c, &record, record.Current.AccessToken)
		return "", errLinear
	}
	record.Current = token
	if l.save("container/"+c, record) != nil {
		return "", errLinear
	}
	if l.retireContainer(ctx, c, &record, token.AccessToken) != nil {
		return "", errLinear
	}
	return token.ExpiresAt, nil
}

// Revoke survives a stopped or absent container. Closed is durable before revocation,
// so maintenance queued behind close cannot issue or deliver another token.
func (l Linear) Revoke(ctx context.Context, c string) error {
	// An existing lock also covers issuance still waiting for its first token.
	unlock, err := l.lock("container/"+c, false)()
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return errLinear
	}
	defer unlock()
	var record linearContainerRecord
	if err := l.read("container/"+c, &record); errors.Is(err, ErrNotFound) {
		return nil
	} else if err != nil {
		return errLinear
	}
	record.Closed = true
	if l.save("container/"+c, record) != nil {
		return errLinear
	}
	return l.retireContainer(ctx, c, &record, "")
}
