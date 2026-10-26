package launcher

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/fixtures"
	"ensemble/broker/internal/protocol"
)

var proxyCalls = []string{"ps --all", "container inspect", "network inspect"}

func labelsEdit(edit func(labels map[string]any)) func(map[string]any) {
	return func(g map[string]any) { edit(g["Config"].(map[string]any)["Labels"].(map[string]any)) }
}

func fixtureManifest(t *testing.T, name string) string {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(fixtures.Dir(), name))
	if err != nil {
		t.Fatal(err)
	}
	return writeManifest(t, data)
}

// credentialHost is startHost with every credential side recording its calls.
func credentialHost(w *world) (Host, *fakeDocker, *fakeCreds, *fakeTokens, *fakeCodexCreds, *fakeKeys, *fakeEnv) {
	creds, tokens, codexCreds, keys, env := &fakeCreds{}, &fakeTokens{}, &fakeCodexCreds{}, &fakeKeys{}, &fakeEnv{}
	h, d := startHost(w, creds)
	h.GitHub, h.Codex, h.Opencode, h.Env = tokens, codexCreds, keys, env
	return h, d, creds, tokens, codexCreds, keys, env
}

// credentialManifest is a fixture manifest by name; withSecretsSuffix adds the environment secrets to it.
func credentialManifest(t *testing.T, name string) string {
	t.Helper()
	if base, ok := strings.CutSuffix(name, withSecretsSuffix); ok {
		return editedFixture(t, base, withSecrets(dummySecrets))
	}
	return fixtureManifest(t, name)
}

const withSecretsSuffix = "+secrets"

func TestEgressRefusals(t *testing.T) {
	two := proxyID + "\n" + strings.Repeat("c", 64) + "\n"
	none, short := "", "abc123\n"
	for _, tc := range []struct {
		name  string
		world world
		calls int    // proxy calls made before refusing
		want  string // in the message
	}{
		{"no proxy", world{proxies: &none}, 1, "No egress proxy container"},
		{"two proxies", world{proxies: &two}, 1, "Found 2 egress proxy containers"},
		{"malformed id", world{proxies: &short}, 1, "Cannot inspect egress proxy egress-proxy"},
		{"unknown contract", world{proxy: labelsEdit(func(l map[string]any) { l["windroc.egress.contract"] = "2" })}, 2, "contract version 2; this broker knows 1"},
		{"no contract label", world{proxy: labelsEdit(func(l map[string]any) { delete(l, "windroc.egress.contract") })}, 2, "contract version"},
		{"stopped", world{proxy: func(g map[string]any) {
			g["State"] = map[string]any{"Running": false, "Health": map[string]any{"Status": "unhealthy"}}
		}}, 2, "is not running"},
		{"paused", world{proxy: func(g map[string]any) { g["State"].(map[string]any)["Paused"] = true }}, 2, "is not running"},
		{"unhealthy", world{proxy: func(g map[string]any) { g["State"].(map[string]any)["Health"] = map[string]any{"Status": "unhealthy"} }}, 2, "is unhealthy, not healthy"},
		{"starting", world{proxy: func(g map[string]any) { g["State"].(map[string]any)["Health"] = map[string]any{"Status": "starting"} }}, 2, "is starting, not healthy"},
		{"no health check", world{proxy: func(g map[string]any) { delete(g["State"].(map[string]any), "Health") }}, 2, "without a health check, not healthy"},
		{"no workers key", world{proxy: labelsEdit(func(l map[string]any) { l["windroc.egress.networks"] = "mini-op" })}, 2, "no valid workers network labels"},
		{"workers key prefix only", world{proxy: labelsEdit(func(l map[string]any) { l["windroc.egress.networks"] = "mini-op,workers2" })}, 2, "no valid workers network labels"},
		{"no name label", world{proxy: labelsEdit(func(l map[string]any) { delete(l, "windroc.egress.network.workers.name") })}, 2, "no valid workers network labels"},
		{"no proxy label", world{proxy: labelsEdit(func(l map[string]any) { delete(l, "windroc.egress.network.workers.proxy") })}, 2, "no valid workers network labels"},
		{"flag as network name", world{proxy: labelsEdit(func(l map[string]any) { l["windroc.egress.network.workers.name"] = "--privileged" })}, 2, "no valid workers network labels"},
		{"https proxy", world{proxy: labelsEdit(func(l map[string]any) {
			l["windroc.egress.network.workers.proxy"] = "https://egress-proxy-workers:4750"
		})}, 2, "no valid workers network labels"},
		{"proxy without port", world{proxy: labelsEdit(func(l map[string]any) { l["windroc.egress.network.workers.proxy"] = "http://egress-proxy-workers" })}, 2, "no valid workers network labels"},
		{"proxy with user", world{proxy: labelsEdit(func(l map[string]any) {
			l["windroc.egress.network.workers.proxy"] = "http://u:p@egress-proxy-workers:4750"
		})}, 2, "no valid workers network labels"},
		{"proxy with path", world{proxy: labelsEdit(func(l map[string]any) {
			l["windroc.egress.network.workers.proxy"] = "http://egress-proxy-workers:4750/x"
		})}, 2, "no valid workers network labels"},
		{"not attached", world{proxy: func(g map[string]any) {
			delete(g["NetworkSettings"].(map[string]any)["Networks"].(map[string]any), workersNetwork)
		}}, 2, "Egress proxy egress-proxy is not attached to network egress-workers"},
		{"network absent", world{noNetwork: true}, 3, "Egress network egress-workers of proxy egress-proxy does not exist"},
		{"network matched by id prefix", world{network: func(n map[string]any) { n["Name"] = "other" }}, 3, "does not exist"},
		{"network not internal", world{network: func(n map[string]any) { n["Internal"] = false }}, 3, "Egress network egress-workers of proxy egress-proxy is not internal"},
		{"another network of that name", world{network: func(n map[string]any) { n["Id"] = strings.Repeat("a", 64) }}, 3, "is not attached to network egress-workers"},
		{"renamed proxy", world{proxy: func(g map[string]any) {
			g["Name"] = "/my-proxy"
			g["State"].(map[string]any)["Health"] = map[string]any{"Status": "unhealthy"}
		}}, 2, "Egress proxy my-proxy is unhealthy"},
	} {
		for _, manifest := range []string{"manifest-github-app.json", "manifest-codex.json", "manifest-opencode-providers.json", "manifest-github-app.json" + withSecretsSuffix} {
			t.Run(tc.name+"/"+manifest, func(t *testing.T) {
				w := tc.world
				w.labels = goodLabels()
				h, d, creds, tokens, codexCreds, keys, env := credentialHost(&w)
				_, err := h.Start(context.Background(), credentialManifest(t, manifest))
				e := protoErr(t, err)
				if e.Code != protocol.EgressUnavailable || protocol.Exit(e.Code) != 17 || e.Cleanup != protocol.NotNeeded {
					t.Fatalf("%+v", e)
				}
				if !strings.Contains(e.Message, tc.want) || !strings.HasSuffix(e.Message, egressHint) ||
					!strings.Contains(e.Message, "workers") || // the caller network, by key or network name
					!strings.Contains(e.Message, "Start the egress proxy (~/.egress/current/start.sh") ||
					(!strings.Contains(e.Message, "egress-proxy") && !strings.Contains(e.Message, "my-proxy")) {
					t.Fatalf("message %q lacks %q, the proxy or ~/.egress/current/start.sh", e.Message, tc.want)
				}
				if got := d.verbs(); !slices.Equal(got, proxyCalls[:tc.calls]) {
					t.Fatalf("docker calls %q, want %q", got, proxyCalls[:tc.calls])
				}
				if len(creds.calls)+len(tokens.calls)+len(keys.calls)+len(env.calls)+codexCreds.ready+codexCreds.injected != 0 {
					t.Fatalf("credential calls: %q %q %q %q codex=%d/%d", creds.calls, tokens.calls, keys.calls, env.calls, codexCreds.ready, codexCreds.injected)
				}
			})
		}
	}
}

func TestEgressDockerFailures(t *testing.T) {
	for _, tc := range []struct {
		name string
		w    world
		want protocol.Code
	}{
		{"unreachable", world{unreachable: true}, protocol.DockerUnreachable},
		{"listing unreachable", world{egressErr: docker.ErrUnreachable}, protocol.DockerUnreachable},
		{"listing fails", world{egressErr: &docker.CommandError{Exit: 1, Stderr: "boom"}}, protocol.EgressUnavailable},
		{"inspect not json", world{proxy: func(g map[string]any) { g["State"] = "running" }}, protocol.EgressUnavailable},
	} {
		t.Run(tc.name, func(t *testing.T) {
			w := tc.w
			w.labels = goodLabels()
			h, d, creds, _, _, _, _ := credentialHost(&w)
			_, err := h.Start(context.Background(), validManifest(t))
			if e := protoErr(t, err); e.Code != tc.want || len(creds.calls) != 0 || hasCall(d, "run") {
				t.Fatalf("%+v %q %q", e, creds.calls, d.verbs())
			}
		})
	}
}

// networkArgs is each --network value in argv.
func networkArgs(argv []string) []string {
	var out []string
	for i, a := range argv {
		if a == "--network" && i+1 < len(argv) {
			out = append(out, argv[i+1])
		}
	}
	return out
}

func envArgs(argv []string) map[string]string {
	out := map[string]string{}
	for i, a := range argv {
		if a == "--env" && i+1 < len(argv) {
			k, v, _ := strings.Cut(argv[i+1], "=")
			out[k] = v
		}
	}
	return out
}

// A start, restart and resume (each a new generation) of every client joins only the proxy's workers network.
func TestStartJoinsOnlyWorkers(t *testing.T) {
	for _, manifest := range []string{"manifest-valid.json", "manifest-codex.json", "manifest-opencode-providers.json", "manifest-github-app.json"} {
		for _, generation := range []string{"", "fedcba9876543210fedcba9876543210"} {
			t.Run(manifest+"/"+generation, func(t *testing.T) {
				w := &world{labels: goodLabels()}
				h, d, _, _, _, _, _ := credentialHost(w)
				path := fixtureManifest(t, manifest)
				if generation != "" {
					path = editedFixture(t, manifest, func(f map[string]any) { f["generation"] = generation })
				}
				if _, err := h.Start(context.Background(), path); err != nil {
					t.Fatal(err)
				}
				run := detached(d)
				if got := networkArgs(run); !slices.Equal(got, []string{workersNetwork}) {
					t.Fatalf("--network values %q in %q", got, run)
				}
				if !slices.Equal(d.verbs()[:3], proxyCalls) || !slices.Equal(d.calls[2], []string{"network", "inspect", workersNetwork}) {
					t.Fatalf("proxy check first: %q", d.verbs())
				}
				if !slices.Equal(d.calls[0], []string{"ps", "--all", "--no-trunc", "--filter", "label=windroc.egress.contract", "--format", "{{.ID}}"}) ||
					!slices.Equal(d.calls[1], []string{"container", "inspect", proxyID}) {
					t.Fatalf("proxy calls %q", d.calls[:2])
				}
			})
		}
	}
}

func editedFixture(t *testing.T, name string, edit func(map[string]any)) string {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(fixtures.Dir(), name))
	if err != nil {
		t.Fatal(err)
	}
	var fields map[string]any
	if err := json.Unmarshal(data, &fields); err != nil {
		t.Fatal(err)
	}
	edit(fields)
	if data, err = json.Marshal(fields); err != nil {
		t.Fatal(err)
	}
	return writeManifest(t, data)
}

func TestStartProxyEnv(t *testing.T) {
	h, d := startHost(&world{labels: goodLabels()}, &fakeCreds{})
	if _, err := h.Start(context.Background(), validManifest(t)); err != nil {
		t.Fatal(err)
	}
	env := envArgs(detached(d))
	for _, key := range []string{"HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"} {
		if env[key] != workersProxy {
			t.Fatalf("%s=%q", key, env[key])
		}
	}
	for _, key := range []string{"NO_PROXY", "no_proxy"} {
		if env[key] != "localhost,127.0.0.1,::1" {
			t.Fatalf("%s=%q", key, env[key])
		}
	}
	for _, key := range []string{"ALL_PROXY", "all_proxy"} {
		if _, ok := env[key]; ok {
			t.Fatalf("%s set", key)
		}
	}
}

// Helpers stay on network none, and no client mount reaches the proxy's config, checkout or a Docker socket.
func TestHelpersStayOffline(t *testing.T) {
	for _, manifest := range []string{"manifest-valid.json", "manifest-codex.json", "manifest-opencode.json"} {
		t.Run(manifest, func(t *testing.T) {
			h, d, _, _, _, _, _ := credentialHost(&world{labels: goodLabels()})
			if _, err := h.Start(context.Background(), fixtureManifest(t, manifest)); err != nil {
				t.Fatal(err)
			}
			helpers := 0
			for _, c := range d.calls {
				if c[0] == "run" && c[1] == "--rm" {
					helpers++
					if got := networkArgs(c); !slices.Equal(got, []string{"none"}) {
						t.Fatalf("helper networks %q in %q", got, c)
					}
				}
			}
			if helpers == 0 {
				t.Fatal("no helper ran")
			}
			run := detached(d)
			for i, a := range run {
				if i > 0 && run[i-1] == "--mount" && (strings.Contains(a, "/.egress") || strings.Contains(a, "docker.sock")) {
					t.Fatalf("mount %q", a)
				}
			}
		})
	}
}

func TestValidProxy(t *testing.T) {
	for proxy, want := range map[string]bool{
		"http://egress-proxy-workers:4750":     true,
		"http://egress-proxy-workers:4750/":    true,
		"http://10.0.0.2:3128":                 true,
		"http://egress-proxy-workers:0":        false,
		"http://egress-proxy-workers:70000":    false,
		"http://:4750":                         false,
		"http://h:4750?x=1":                    false,
		"http://h:4750#x":                      false,
		"socks5://h:1080":                      false,
		"egress-proxy-workers:4750":            false,
		"http://h:4750\n--privileged":          false,
		"http://h:" + strings.Repeat("1", 300): false,
	} {
		if got := validProxy(proxy); got != want {
			t.Errorf("validProxy(%q) = %v", proxy, got)
		}
	}
}

func TestEgressListingIgnoresBlankLines(t *testing.T) {
	out := "\n" + proxyID + "\n\n"
	w := &world{labels: goodLabels(), proxies: &out}
	h, _ := startHost(w, &fakeCreds{})
	if _, err := h.Start(context.Background(), validManifest(t)); err != nil {
		t.Fatal(err)
	}
}

// A manifest's gateway picks the caller network: the container joins it alone, with its proxy.
func TestStartOnCallerNetwork(t *testing.T) {
	h, d, _, _, _, _, _ := credentialHost(&world{labels: goodLabels()})
	path := editedFixture(t, "manifest-valid.json", func(f map[string]any) { f["gateway"] = "mini-op" })
	if _, err := h.Start(context.Background(), path); err != nil {
		t.Fatal(err)
	}
	run := detached(d)
	if got := networkArgs(run); !slices.Equal(got, []string{miniOpNetwork}) {
		t.Fatalf("--network values %q in %q", got, run)
	}
	env := envArgs(run)
	for _, key := range []string{"HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"} {
		if env[key] != miniOpProxy {
			t.Fatalf("%s=%q", key, env[key])
		}
	}
	if !slices.Equal(d.verbs()[:3], proxyCalls) || !slices.Equal(d.calls[2], []string{"network", "inspect", miniOpNetwork}) {
		t.Fatalf("proxy check first: %q", d.calls[:3])
	}
}

// A gateway key the egress proxy does not serve validly is egress_unavailable naming the key, never busy.
func TestEgressRefusalsByKey(t *testing.T) {
	for _, tc := range []struct {
		name    string
		gateway string
		world   world
		calls   int
		want    string
	}{
		{"unlisted", "nope", world{}, 2, "has no valid nope network labels"},
		{"listed without labels", "nope", world{proxy: labelsEdit(func(l map[string]any) { l["windroc.egress.networks"] = "mini-op,workers,nope" })}, 2,
			"has no valid nope network labels"},
		{"bad proxy", "mini-op", world{proxy: labelsEdit(func(l map[string]any) { l["windroc.egress.network.mini-op.proxy"] = "https://x:1" })}, 2,
			"has no valid mini-op network labels"},
		{"not attached", "mini-op", world{proxy: func(g map[string]any) {
			delete(g["NetworkSettings"].(map[string]any)["Networks"].(map[string]any), miniOpNetwork)
		}}, 2, "is not attached to network egress-mini-op"},
		{"not internal", "mini-op", world{network: func(n map[string]any) { n["Internal"] = false }}, 3, "Egress network egress-mini-op of proxy egress-proxy is not internal"},
		{"proxy stopped", "mini-op", world{proxy: func(g map[string]any) {
			g["State"] = map[string]any{"Running": false, "Health": map[string]any{"Status": "unhealthy"}}
		}}, 2, "Egress proxy egress-proxy is not running. Caller network mini-op needs it."},
		{"no proxy", "mini-op", world{proxies: new(string)}, 1, "Caller network mini-op needs it."},
	} {
		t.Run(tc.name, func(t *testing.T) {
			w := tc.world
			w.labels = goodLabels()
			h, d, creds, _, _, _, _ := credentialHost(&w)
			path := editedFixture(t, "manifest-valid.json", func(f map[string]any) { f["gateway"] = tc.gateway })
			_, err := h.Start(context.Background(), path)
			e := protoErr(t, err)
			if e.Code != protocol.EgressUnavailable || e.Code == protocol.Busy || e.Holder != nil {
				t.Fatalf("%+v", e)
			}
			for _, want := range []string{tc.want, "egress-proxy", "~/.egress/current/start.sh"} {
				if !strings.Contains(e.Message, want) {
					t.Fatalf("message %q lacks %q", e.Message, want)
				}
			}
			if got := d.verbs(); !slices.Equal(got, proxyCalls[:tc.calls]) || len(creds.calls) != 0 {
				t.Fatalf("docker calls %q, credential calls %q", got, creds.calls)
			}
		})
	}
}
