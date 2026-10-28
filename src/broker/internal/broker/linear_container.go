package broker

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/url"
	"slices"
	"strings"
	"time"

	"ensemble/broker/internal/audit"
	"ensemble/broker/internal/credential"
	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/protocol"
)

func roleIdentity(a credential.LinearApp) LinearIdentity {
	return LinearIdentity{Name: a.Role, Kind: "role", ClientID: a.ClientID, UserID: a.UserID, SecretService: a.SecretService, TokenService: a.TokenService}
}

// containerAccount accepts only the exact active role binding enrolled on the host.
func (l Linear) containerAccount(app credential.LinearApp) (linearAccount, error) {
	a := roleIdentity(app)
	if a.Name == "manager" || a.Name == "harness" || a.check() != nil {
		return linearAccount{}, errLinear
	}
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

func linearCommand(token linearAccess, replace bool) (credential.Command, error) {
	data, _ := json.Marshal(token)
	defer erase(data)
	return credential.Prepare(credential.File{Dir: credential.LinearDir, Name: "token.json", Replace: replace}, data)
}

func (l Linear) noteContainer(c, result, reason string, token linearAccess) {
	l.Audit.Add(c, audit.Credential{Kind: audit.LinearAccessToken, Result: result, Reason: reason, ExpiresAt: &token.ExpiresAt})
}

func (l Linear) revokeContainerToken(ctx context.Context, c string, token linearAccess) {
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
}

func (l Linear) Issue(ctx context.Context, r credential.LinearRequest, c string) ([]credential.Command, func(), string, error) {
	token, err := l.containerToken(ctx, r.App)
	if err != nil {
		return nil, nil, "", errLinear
	}
	l.noteContainer(c, audit.Issued, "", token)
	revoke := func() { l.revokeContainerToken(ctx, c, token) }
	command, err := linearCommand(token, false)
	if err != nil {
		revoke()
		return nil, nil, "", errLinear
	}
	board, _ := json.Marshal(r.Board)
	defer erase(board)
	config, err := credential.Prepare(credential.File{Dir: credential.LinearDir, Name: "board.json"}, board)
	if err != nil {
		command.Erase()
		revoke()
		return nil, nil, "", errLinear
	}
	return []credential.Command{command, config}, revoke, token.ExpiresAt, nil
}

func readLinearToken(ctx context.Context, read func(context.Context, []string) ([]byte, error)) (linearAccess, error) {
	data, err := read(ctx, []string{"/bin/sh", "-c", "if [ -f " + credential.LinearDir + "/token.json ]; then head -c 65537 " + credential.LinearDir + "/token.json; fi"})
	defer erase(data)
	if docker.Absent(err) {
		return linearAccess{}, nil
	}
	if err != nil {
		return linearAccess{}, errLinear
	}
	if len(data) == 0 {
		return linearAccess{}, nil
	}
	var token linearAccess
	if len(data) > 65536 || json.Unmarshal(data, &token) != nil || !linearClean(token.AccessToken) {
		return linearAccess{}, errLinear
	}
	if _, err := time.Parse(time.RFC3339, token.ExpiresAt); err != nil {
		return linearAccess{}, errLinear
	}
	return token, nil
}

func (l Linear) Refresh(ctx context.Context, r credential.LinearRequest, c string, exec func([]string, []byte) error, read func(context.Context, []string) ([]byte, error)) (string, error) {
	old, err := readLinearToken(ctx, read)
	if err != nil {
		return "", errLinear
	}
	token, err := l.containerToken(ctx, r.App)
	if err != nil {
		return "", errLinear
	}
	l.noteContainer(c, audit.Renewed, "", token)
	command, err := linearCommand(token, true)
	if err == nil {
		defer command.Erase()
		err = exec(command.Argv, command.Stdin)
	}
	if err != nil {
		l.revokeContainerToken(ctx, c, token)
		return "", errLinear
	}
	if old.AccessToken != "" {
		l.revokeContainerToken(ctx, c, old)
	}
	return token.ExpiresAt, nil
}

// Revoke reads only the container copy, then leaves removal to the launcher even on failure.
func (l Linear) Revoke(ctx context.Context, c string, read func(context.Context, []string) ([]byte, error)) {
	rctx, cancel := detached(ctx)
	token, err := readLinearToken(rctx, read)
	cancel()
	if err != nil {
		l.noteContainer(c, audit.Unrevoked, "token file unreadable", linearAccess{})
		return
	}
	if token.AccessToken != "" {
		l.revokeContainerToken(ctx, c, token)
	}
}
