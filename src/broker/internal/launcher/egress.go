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

// The egress gateway's label contract, version 1 (windroc/egress-proxy docs/label-contract.md): the broker finds the
// gateway and its workers network only through these labels and Docker health, and never probes the gateway.
const (
	egressPrefix    = "windroc.egress."
	contractLabel   = egressPrefix + "contract"
	contractVersion = "1"
	networksLabel   = egressPrefix + "networks"
	workersKey      = "workers"
	gatewayName     = "egress-gateway"
	noProxy         = "localhost,127.0.0.1,::1"
	egressHint      = " Start the egress gateway (./start.sh in its egress-proxy checkout, docs/docker-host.md#egress-gateway), then retry."
)

// networkPattern is a Docker network name; never a flag.
var networkPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$`)

// egress is the gateway check's result: the network a client container joins and the proxy URL it uses there.
type egress struct{ Network, Proxy string }

type gatewayInfo struct {
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

// workers is the workers network and proxy the gateway's labels name, if valid.
func workers(labels map[string]string) (egress, bool) {
	keys := strings.Split(labels[networksLabel], ",")
	for i := range keys {
		keys[i] = strings.TrimSpace(keys[i])
	}
	e := egress{Network: labels[egressPrefix+"network."+workersKey+".name"], Proxy: labels[egressPrefix+"network."+workersKey+".proxy"]}
	return e, slices.Contains(keys, workersKey) && networkPattern.MatchString(e.Network) && validProxy(e.Proxy)
}

func inspectOne[T any](out []byte) (T, error) {
	var found []T
	if err := json.Unmarshal(out, &found); err != nil || len(found) != 1 {
		var zero T
		return zero, errors.New("invalid inspection")
	}
	return found[0], nil
}

// checkEgress applies the contract's consumer rules for the workers network. It runs before any credential or Docker
// object is touched, so a refusal leaves nothing to withdraw or remove; there is no fallback network.
func (h Host) checkEgress(ctx context.Context) (egress, error) {
	out, err := h.Docker.Run(ctx, []string{"ps", "--all", "--no-trunc", "--filter", "label=" + contractLabel, "--format", "{{.ID}}"}, nil)
	if err != nil {
		return egress{}, failure(err, protocol.EgressUnavailable, "Cannot list egress gateway containers."+egressHint)
	}
	ids := strings.Fields(string(out))
	switch {
	case len(ids) == 0:
		return egress{}, refuse("No egress gateway container (label %s) found; expected %s.", contractLabel, gatewayName)
	case len(ids) > 1:
		return egress{}, refuse("Found %d egress gateway containers (label %s); keep only %s.", len(ids), contractLabel, gatewayName)
	case !containerIDPattern.MatchString(ids[0]):
		return egress{}, refuse("Cannot inspect egress gateway %s.", gatewayName)
	}
	out, err = h.Docker.Run(ctx, []string{"container", "inspect", ids[0]}, nil)
	if err != nil {
		return egress{}, failure(err, protocol.EgressUnavailable, "Cannot inspect egress gateway "+gatewayName+"."+egressHint)
	}
	gw, err := inspectOne[gatewayInfo](out)
	if err != nil {
		return egress{}, refuse("Cannot inspect egress gateway %s.", gatewayName)
	}
	name := strings.TrimPrefix(gw.Name, "/")
	if !networkPattern.MatchString(name) {
		name = gatewayName
	}
	labels := gw.Config.Labels
	switch {
	case labels[contractLabel] != contractVersion:
		return egress{}, refuse("Egress gateway %s has label contract version %s; this broker knows %s.", name, clip(labels[contractLabel]), contractVersion)
	case !gw.State.Running || gw.State.Paused: // a paused gateway keeps its last health but proxies nothing
		return egress{}, refuse("Egress gateway %s is not running.", name)
	case gw.State.Health == nil:
		return egress{}, refuse("Egress gateway %s is without a health check, not healthy.", name)
	case gw.State.Health.Status != "healthy":
		return egress{}, refuse("Egress gateway %s is %s, not healthy.", name, clip(gw.State.Health.Status))
	}
	e, ok := workers(labels)
	if !ok {
		return egress{}, refuse("Egress gateway %s has no valid workers network labels.", name)
	}
	attached, ok := gw.NetworkSettings.Networks[e.Network]
	if !ok || attached.NetworkID == "" {
		return egress{}, refuse("Egress gateway %s is not attached to network %s.", name, e.Network)
	}
	out, err = h.Docker.Run(ctx, []string{"network", "inspect", e.Network}, nil)
	if docker.Absent(err) {
		return egress{}, refuse("Egress network %s of gateway %s does not exist.", e.Network, name)
	}
	if err != nil {
		return egress{}, failure(err, protocol.EgressUnavailable, "Cannot inspect egress network "+e.Network+" of gateway "+name+"."+egressHint)
	}
	network, err := inspectOne[networkInfo](out)
	switch {
	case err != nil:
		return egress{}, refuse("Cannot inspect egress network %s of gateway %s.", e.Network, name)
	case network.Name != e.Network: // Docker matched an ID prefix, not the name
		return egress{}, refuse("Egress network %s of gateway %s does not exist.", e.Network, name)
	case !network.Internal:
		return egress{}, refuse("Egress network %s of gateway %s is not internal.", e.Network, name)
	case network.ID != attached.NetworkID:
		return egress{}, refuse("Egress gateway %s is not attached to network %s.", name, e.Network)
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
