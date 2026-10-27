package launcher

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"regexp"
	"slices"
	"strconv"
	"strings"

	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/protocol"
)

// The egress proxy's label contract, version 1 (windroc-labs/egress-proxy docs/label-contract.md): the broker finds the
// proxy and each caller network it serves only through these labels and Docker health, and never probes the proxy.
const (
	egressPrefix    = "windroc.egress."
	contractLabel   = egressPrefix + "contract"
	contractVersion = "1"
	networksLabel   = egressPrefix + "networks"
	workersKey      = "workers"
	proxyName       = "egress-proxy"
	noProxy         = "localhost,127.0.0.1,::1"
	egressHint      = " Start the egress proxy (~/.egress/current/start.sh, docs/architecture.md#egress-proxy), then retry."
)

// networkPattern is a Docker network name; never a flag.
var networkPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$`)

// egress is a caller network check's result: the network a client container joins and the proxy URL it uses there.
type egress struct{ Network, Proxy string }

// egressProxy is one start's snapshot of the healthy egress proxy: its name, labels and attached networks. Every caller network
// a start uses is checked against the same snapshot.
type egressProxy struct {
	name     string
	labels   map[string]string
	attached map[string]string // network name -> network ID
}

type proxyInfo struct {
	ID     string `json:"Id"`
	Name   string
	Config struct{ Labels map[string]string }
	State  struct {
		Running, Paused bool
		Health          *struct{ Status string }
	}
	NetworkSettings struct {
		Networks map[string]struct{ NetworkID string }
	}
}

type networkInfo struct {
	Name     string
	ID       string `json:"Id"`
	Internal bool
}

func refuse(format string, args ...any) *protocol.Error {
	return protocol.New(protocol.EgressUnavailable, fmt.Sprintf(format, args...)+egressHint)
}

// clip bounds a label value quoted in a message, on a rune boundary.
func clip(s string) string {
	if r := []rune(s); len(r) > 32 {
		return string(r[:32])
	}
	return s
}

// validProxy is whether s is an http://host:port proxy URL, as the contract defines it.
func validProxy(s string) bool {
	u, err := url.Parse(s)
	if len(s) > 256 || err != nil || u.Scheme != "http" || u.User != nil || u.Opaque != "" || u.Hostname() == "" ||
		(u.Path != "" && u.Path != "/") || u.RawQuery != "" || u.ForceQuery || u.Fragment != "" {
		return false
	}
	port, err := strconv.Atoi(u.Port())
	return err == nil && port > 0 && port <= 65535
}

// callerLabels is the network and proxy the proxy's labels name for caller network key, if listed and valid.
func callerLabels(labels map[string]string, key string) (egress, bool) {
	keys := strings.Split(labels[networksLabel], ",")
	for i := range keys {
		keys[i] = strings.TrimSpace(keys[i])
	}
	e := egress{Network: labels[egressPrefix+"network."+key+".name"], Proxy: labels[egressPrefix+"network."+key+".proxy"]}
	return e, slices.Contains(keys, key) && networkPattern.MatchString(e.Network) && validProxy(e.Proxy)
}

func inspectOne[T any](out []byte) (T, error) {
	var found []T
	if err := json.Unmarshal(out, &found); err != nil || len(found) != 1 {
		var zero T
		return zero, errors.New("invalid inspection")
	}
	return found[0], nil
}

// checkProxy applies the contract's consumer rules to the egress proxy itself: exactly one, its contract version, running
// and healthy. Start runs it before any credential or Docker object is touched, so a refusal leaves nothing to
// withdraw or remove; there is no fallback network. A refusal names caller network key, the one start needs.
func (h Host) checkProxy(ctx context.Context, key string) (egressProxy, error) {
	px, err := h.inspectProxy(ctx)
	var e *protocol.Error
	if errors.As(err, &e) && e.Code == protocol.EgressUnavailable {
		e.Message = strings.TrimSuffix(e.Message, egressHint) + " Caller network " + key + " needs it." + egressHint
	}
	return px, err
}

func (h Host) inspectProxy(ctx context.Context) (egressProxy, error) {
	out, err := h.Docker.Run(ctx, []string{"ps", "--all", "--no-trunc", "--filter", "label=" + contractLabel, "--format", "{{.ID}}"}, nil)
	if err != nil {
		return egressProxy{}, failure(err, protocol.EgressUnavailable, "Cannot list egress proxy containers."+egressHint)
	}
	ids := strings.Fields(string(out))
	switch {
	case len(ids) == 0:
		return egressProxy{}, refuse("No egress proxy container (label %s) found; expected %s.", contractLabel, proxyName)
	case len(ids) > 1:
		return egressProxy{}, refuse("Found %d egress proxy containers (label %s); keep only %s.", len(ids), contractLabel, proxyName)
	case !containerIDPattern.MatchString(ids[0]):
		return egressProxy{}, refuse("Cannot inspect egress proxy %s.", proxyName)
	}
	out, err = h.Docker.Run(ctx, []string{"container", "inspect", ids[0]}, nil)
	if err != nil {
		return egressProxy{}, failure(err, protocol.EgressUnavailable, "Cannot inspect egress proxy "+proxyName+"."+egressHint)
	}
	px, err := inspectOne[proxyInfo](out)
	if err != nil {
		return egressProxy{}, refuse("Cannot inspect egress proxy %s.", proxyName)
	}
	name := strings.TrimPrefix(px.Name, "/")
	if !networkPattern.MatchString(name) {
		name = proxyName
	}
	labels := px.Config.Labels
	switch {
	case labels[contractLabel] != contractVersion:
		return egressProxy{}, refuse("Egress proxy %s has label contract version %s; this broker knows %s.", name, clip(labels[contractLabel]), contractVersion)
	case !px.State.Running || px.State.Paused: // a paused proxy keeps its last health but proxies nothing
		return egressProxy{}, refuse("Egress proxy %s is not running.", name)
	case px.State.Health == nil:
		return egressProxy{}, refuse("Egress proxy %s is without a health check, not healthy.", name)
	case px.State.Health.Status != "healthy":
		return egressProxy{}, refuse("Egress proxy %s is %s, not healthy.", name, clip(px.State.Health.Status))
	}
	attached := map[string]string{}
	for network, a := range px.NetworkSettings.Networks {
		attached[network] = a.NetworkID
	}
	return egressProxy{name: name, labels: labels, attached: attached}, nil
}

// caller applies the contract's consumer rules for caller network key on px: listed with a valid network and proxy,
// that network internal and px attached to it.
func (h Host) caller(ctx context.Context, px egressProxy, key string) (egress, error) {
	name := px.name
	e, ok := callerLabels(px.labels, key)
	if !ok {
		return egress{}, refuse("Egress proxy %s has no valid %s network labels.", name, key)
	}
	attached := px.attached[e.Network]
	if attached == "" {
		return egress{}, refuse("Egress proxy %s is not attached to network %s.", name, e.Network)
	}
	out, err := h.Docker.Run(ctx, []string{"network", "inspect", e.Network}, nil)
	if docker.Absent(err) {
		return egress{}, refuse("Egress network %s of proxy %s does not exist.", e.Network, name)
	}
	if err != nil {
		return egress{}, failure(err, protocol.EgressUnavailable, "Cannot inspect egress network "+e.Network+" of proxy "+name+"."+egressHint)
	}
	network, err := inspectOne[networkInfo](out)
	switch {
	case err != nil:
		return egress{}, refuse("Cannot inspect egress network %s of proxy %s.", e.Network, name)
	case network.Name != e.Network: // Docker matched an ID prefix, not the name
		return egress{}, refuse("Egress network %s of proxy %s does not exist.", e.Network, name)
	case !network.Internal:
		return egress{}, refuse("Egress network %s of proxy %s is not internal.", e.Network, name)
	case network.ID != attached:
		return egress{}, refuse("Egress proxy %s is not attached to network %s.", name, e.Network)
	}
	return e, nil
}

// args are create's flags for e: only its network, and the proxy variables in both cases.
func (e egress) args() []string {
	args := []string{"--network", e.Network}
	for _, key := range []string{"HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"} {
		args = append(args, "--env", key+"="+e.Proxy)
	}
	return append(args, "--env", "NO_PROXY="+noProxy, "--env", "no_proxy="+noProxy)
}
