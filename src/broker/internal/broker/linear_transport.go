package broker

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"time"

	"ensemble/broker/internal/credential"
)

// transportToken copies only the existing access token. Its owner renews it on the
// host; container close must not revoke a grant still used by that owner or another run.
func (l Linear) transportToken(ctx context.Context, app credential.LinearApp) (linearAccess, error) {
	a := roleIdentity(app)
	if a.check() != nil || a.Name == "harness" || a.Name == "manager" {
		return linearAccess{}, errLinear
	}
	account, err := l.keys.AppAccount(app.TokenService)
	if err != nil {
		return linearAccess{}, errLinear
	}
	data, err := l.host.Read(app.TokenService, account)
	defer erase(data)
	var token linearAccess
	if err != nil || len(data) > 65536 || json.Unmarshal(data, &token) != nil || !linearClean(token.AccessToken) {
		return linearAccess{}, errLinear
	}
	expiry, err := time.Parse(time.RFC3339, token.ExpiresAt)
	if err != nil || !l.now().Before(expiry) {
		return linearAccess{}, errLinear
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, "https://api.linear.app/graphql", strings.NewReader(`{"query":"{ viewer { id } }"}`))
	if err != nil {
		return linearAccess{}, errLinear
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+token.AccessToken)
	resp, err := l.http.Do(req)
	if err != nil {
		return linearAccess{}, errLinear
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 65537))
	var result struct {
		Data struct {
			Viewer struct {
				ID string `json:"id"`
			} `json:"viewer"`
		} `json:"data"`
		Errors []json.RawMessage `json:"errors"`
	}
	if err != nil || len(body) > 65536 || resp.StatusCode != http.StatusOK || json.Unmarshal(body, &result) != nil || len(result.Errors) > 0 || result.Data.Viewer.ID != app.UserID || !l.now().Before(expiry) {
		return linearAccess{}, errLinear
	}
	return token, nil
}
