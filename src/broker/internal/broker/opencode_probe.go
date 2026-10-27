package broker

import (
	"context"
	"net/http"
	"net/url"
	"strings"
	"time"

	"ensemble/broker/internal/launcher"
	"ensemble/broker/internal/protocol"
)

// ValidOpencodeURL accepts a configured provider endpoint, without embedded credentials or query data.
func ValidOpencodeURL(base string) bool {
	u, err := url.Parse(base)
	return err == nil && (u.Scheme == "http" || u.Scheme == "https") && u.Hostname() != "" && u.User == nil &&
		u.RawQuery == "" && !u.ForceQuery && !strings.Contains(base, "#") &&
		!strings.ContainsFunc(base, func(r rune) bool { return r < 0x20 || r == 0x7f })
}

// Probe performs the authenticated availability request without exporting a key or response body. It first checks
// the key as start does (Ready), so a key start would refuse fails here too.
// Transport is nil in production; tests provide a fake transport without sockets.
func (o OpencodeKeys) Probe(ctx context.Context, provider, base string, transport http.RoundTripper) (bool, error) {
	if !launcher.ValidProvider(provider) || !ValidOpencodeURL(base) {
		return false, protocol.New(protocol.Usage, "Invalid OpenCode provider probe input.")
	}
	if err := o.Ready([]string{provider}); err != nil {
		return false, err
	}
	key, err := o.Keychain.Secret(opencodeService, provider)
	defer erase(key)
	if err != nil {
		return false, opencodeError(provider, err)
	}
	if checkSecret("API key", key) != nil {
		return false, protocol.New(protocol.LoginRequired, "Invalid OpenCode provider API key; log in again.")
	}
	req, err := http.NewRequestWithContext(ctx, "GET", strings.TrimRight(base, "/")+"/models", nil)
	if err != nil {
		return false, nil
	}
	req.Header.Set("Authorization", "Bearer "+string(key))
	defer req.Header.Del("Authorization")
	client := &http.Client{Timeout: 5 * time.Second, Transport: transport, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	res, err := client.Do(req)
	if err != nil {
		return false, nil
	}
	defer res.Body.Close()
	return res.StatusCode >= 200 && res.StatusCode < 300, nil
}
