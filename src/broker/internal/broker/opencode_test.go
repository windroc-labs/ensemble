package broker

import (
	"bytes"
	"errors"
	"reflect"
	"slices"
	"strings"
	"testing"

	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/protocol"
)

const apiKey = "sk-strata-SECRET"

// fakeSecrets is broker-only items keyed service/account, beside one item's access (fakeApps'); readBack replaces
// what a read returns when set.
type fakeSecrets struct {
	fakeApps
	items    map[string][]byte
	readBack []byte
	readErr  error
	putErr   error
	puts     []string
	reads    []string
	returned [][]byte
}

// AppAccess is fakeApps' for an item that exists, else ErrNotFound.
func (k *fakeSecrets) AppAccess(service, account string) (AppAccess, error) {
	if _, ok := k.items[service+"/"+account]; !ok {
		return AppAccess{}, ErrNotFound
	}
	return k.fakeApps.AppAccess(service, account)
}

func (k *fakeSecrets) Secret(service, account string) ([]byte, error) {
	k.reads = append(k.reads, service+"/"+account)
	if k.readErr != nil {
		return nil, k.readErr
	}
	data, ok := k.items[service+"/"+account]
	if !ok {
		return nil, ErrNotFound
	}
	b := append([]byte(nil), data...)
	if k.readBack != nil {
		b = append([]byte(nil), k.readBack...)
	}
	k.returned = append(k.returned, b)
	return b, nil
}

func (k *fakeSecrets) PutSecret(service, account string, data []byte) error {
	k.puts = append(k.puts, service+"/"+account)
	if k.putErr != nil {
		return k.putErr
	}
	if k.items == nil {
		k.items = map[string][]byte{}
	}
	k.items[service+"/"+account] = append([]byte(nil), data...)
	return nil
}

func (k *fakeSecrets) DeleteSecret(service, account string) error {
	delete(k.items, service+"/"+account)
	return nil
}

// loginOpencode runs LoginOpencode for provider strata with key; it fails t when a secret reaches an error or output.
func loginOpencode(t *testing.T, k *fakeSecrets, ask *askOnce, key string) (string, error) {
	t.Helper()
	var out bytes.Buffer
	read := []byte(key)
	err := LoginOpencode("strata", func() ([]byte, error) { return read, nil }, ask.read, k, &out)
	text := out.String()
	if err != nil {
		text += err.Error()
	}
	if key != "" && strings.Contains(text, key) || strings.Contains(text, keychainPass) {
		t.Fatalf("secret in %q", text)
	}
	if !bytes.Equal(read, make([]byte, len(read))) {
		t.Fatal("key buffer not erased")
	}
	for _, b := range k.returned {
		if !bytes.Equal(b, make([]byte, len(b))) {
			t.Fatal("read buffer not erased")
		}
	}
	return out.String(), err
}

func TestLoginOpencode(t *testing.T) {
	k := &fakeSecrets{}
	ask := &askOnce{}
	out, err := loginOpencode(t, k, ask, apiKey)
	if err != nil || out != "OpenCode provider strata API key saved; keychain trusts only the broker\n" {
		t.Fatalf("%v %q", err, out)
	}
	if string(k.items["ensemble-broker-opencode/strata"]) != apiKey || !slices.Equal(k.puts, []string{"ensemble-broker-opencode/strata"}) {
		t.Fatalf("%q %q", k.items, k.puts)
	}
	if ask.calls != 0 || len(k.steps) != 0 {
		t.Fatalf("asked %d, steps %q", ask.calls, k.steps)
	}
}

func TestLoginOpencodeRefusesKey(t *testing.T) {
	for _, bad := range []string{"", "has space", "tab\there", "nul\x00", "new\nline", strings.Repeat("a", 16385)} {
		k := &fakeSecrets{}
		ask := &askOnce{}
		if _, err := loginOpencode(t, k, ask, bad); err == nil || len(k.puts) != 0 || ask.calls != 0 {
			t.Fatalf("%q accepted: %v", bad, err)
		}
	}
	var out bytes.Buffer
	err := LoginOpencode("strata", func() ([]byte, error) { return nil, errors.New("needs an interactive terminal") }, (&askOnce{}).read, &fakeSecrets{}, &out)
	if err == nil || err.Error() != "reading the API key failed: needs an interactive terminal" {
		t.Fatalf("read error: %v", err)
	}
}

func TestLoginOpencodeRefusesProvider(t *testing.T) {
	for _, bad := range []string{"", "ollama", "-x", ".x", "a/b", "a b", strings.Repeat("p", 65)} {
		k := &fakeSecrets{}
		var out bytes.Buffer
		if err := LoginOpencode(bad, func() ([]byte, error) { return []byte(apiKey), nil }, (&askOnce{}).read, k, &out); err == nil || len(k.puts) != 0 {
			t.Fatalf("%q accepted: %v", bad, err)
		}
	}
}

// The item PutSecret makes still needs its partition list: secureItem asks the password once, then the read proves it.
func TestLoginOpencodeTrustsOnlyBroker(t *testing.T) {
	for name, tc := range map[string]struct {
		access       *AppAccess
		partitionErr error
		steps        []string
		fails        string
	}{
		"partition list": {access: halfDone(), steps: []string{"partition ensemble-broker-opencode strata"}},
		"refused": {access: halfDone(), partitionErr: errors.New("keychain status -25293"), steps: []string{"partition ensemble-broker-opencode strata"},
			fails: "rerun ensemble-broker login opencode strata to finish"},
	} {
		k := &fakeSecrets{fakeApps: fakeApps{access: tc.access, partitionErr: tc.partitionErr}}
		ask := &askOnce{}
		_, err := loginOpencode(t, k, ask, apiKey)
		if ask.calls != 1 || !slices.Equal(k.steps, tc.steps) {
			t.Fatalf("%s: asked %d, steps %q", name, ask.calls, k.steps)
		}
		if tc.fails == "" && err != nil || tc.fails != "" && (err == nil || !strings.HasSuffix(err.Error(), tc.fails) ||
			!strings.Contains(err.Error(), "keychain item ensemble-broker-opencode")) {
			t.Fatalf("%s: %v", name, err)
		}
	}
}

func TestLoginOpencodeReadsBack(t *testing.T) {
	for name, tc := range map[string]struct {
		k     *fakeSecrets
		fails string
	}{
		"other bytes": {&fakeSecrets{readBack: []byte("sk-other")},
			"keychain item ensemble-broker-opencode strata: reading it failed: it holds other bytes; rerun ensemble-broker login opencode strata"},
		"needs dialog": {&fakeSecrets{readErr: ErrNeedsDialog},
			"keychain item ensemble-broker-opencode strata: reading it failed: keychain status -25308; rerun ensemble-broker login opencode strata"},
		"put fails": {&fakeSecrets{putErr: errors.New("keychain status -25299")},
			"keychain item ensemble-broker-opencode strata: saving it failed: keychain status -25299; rerun ensemble-broker login opencode strata"},
	} {
		if _, err := loginOpencode(t, tc.k, &askOnce{}, apiKey); err == nil || err.Error() != tc.fails {
			t.Fatalf("%s: %v", name, err)
		}
	}
}

// providerKeys is a keychain holding an API key for each provider, its value apiKey-<provider>.
func providerKeys(providers ...string) *fakeSecrets {
	k := &fakeSecrets{items: map[string][]byte{}}
	for _, p := range providers {
		k.items["ensemble-broker-opencode/"+p] = []byte(apiKey + "-" + p)
	}
	return k
}

func TestOpencodeKeysReady(t *testing.T) {
	for name, tc := range map[string]struct {
		k       *fakeSecrets
		code    protocol.Code
		message string
	}{
		"all":     {k: providerKeys("strata", "lab")},
		"missing": {k: providerKeys("strata"), code: protocol.LoginRequired, message: "No API key for OpenCode provider lab: run ensemble-broker login opencode lab."},
		"earlier build": {k: func() *fakeSecrets { k := providerKeys("strata", "lab"); k.access = reinstalled(); return k }(), code: protocol.LoginRequired,
			message: "The OpenCode provider strata API key trusts an earlier broker build: run " + RetrustCommand + "."},
		"other application": {k: func() *fakeSecrets { k := providerKeys("strata", "lab"); k.access = added(); return k }(), code: protocol.LoginRequired,
			message: "The OpenCode provider strata API key trusts another broker build: run ensemble-broker login opencode strata."},
		"keychain": {k: func() *fakeSecrets {
			k := providerKeys("strata", "lab")
			k.accessErr = errors.New("user denied")
			return k
		}(),
			code: protocol.KeychainFailed, message: "Keychain access to the OpenCode provider strata API key failed."},
	} {
		err := OpencodeKeys{Keychain: tc.k}.Ready([]string{"strata", "lab"})
		var e *protocol.Error
		if tc.code == "" && err != nil || tc.code != "" && (!errors.As(err, &e) || e.Code != tc.code || e.Message != tc.message || e.Cause != "") {
			t.Fatalf("%s: %v", name, err)
		}
		if len(tc.k.returned) != 0 {
			t.Fatalf("%s: Ready read a key", name)
		}
	}
}

func TestOpencodeKeysInject(t *testing.T) {
	e := &fakeExec{}
	k := providerKeys("strata", "lab")
	if err := (OpencodeKeys{Keychain: k}).Inject([]string{"strata", "lab"}, e.run); err != nil {
		t.Fatal(err)
	}
	script := "mkdir -p -m 0700 " + docker.SecretsDir + "/opencode && umask 0377 && set -C && cat > " + docker.SecretsDir + `/opencode/"$1"`
	want := [][]string{{"/bin/sh", "-c", script, "sh", "strata.key"}, {"/bin/sh", "-c", script, "sh", "lab.key"}}
	if !reflect.DeepEqual(e.argv, want) || string(e.stdin[0]) != apiKey+"-strata" || string(e.stdin[1]) != apiKey+"-lab" {
		t.Fatalf("%q %q", e.argv, e.stdin)
	}
	for _, b := range k.returned {
		if !bytes.Equal(b, make([]byte, len(b))) {
			t.Fatal("read buffer not erased")
		}
	}
	for name, tc := range map[string]struct {
		k           *fakeSecrets
		dockerErr   error
		code        protocol.Code
		unreachable bool
	}{
		"docker fails": {k: providerKeys("strata"), dockerErr: &docker.CommandError{Exit: 1, Stderr: "cat: " + apiKey}},
		"unreachable":  {k: providerKeys("strata"), dockerErr: errors.Join(docker.ErrUnreachable, errors.New(apiKey)), unreachable: true},
		"missing":      {k: providerKeys(), code: protocol.LoginRequired},
		"needs dialog": {k: func() *fakeSecrets { k := providerKeys("strata"); k.readErr = ErrNeedsDialog; return k }(), code: protocol.KeychainFailed},
		"keychain fail": {k: func() *fakeSecrets {
			k := providerKeys("strata")
			k.readErr = errors.New("keychain status -25293")
			return k
		}(), code: protocol.KeychainFailed},
	} {
		e := &fakeExec{err: tc.dockerErr}
		err := OpencodeKeys{Keychain: tc.k}.Inject([]string{"strata"}, e.run)
		var pe *protocol.Error
		var ce *docker.CommandError
		if err == nil || strings.Contains(err.Error(), apiKey) || errors.Is(err, docker.ErrUnreachable) != tc.unreachable || errors.As(err, &ce) {
			t.Fatalf("%s: %v", name, err)
		}
		if tc.code != "" && (!errors.As(err, &pe) || pe.Code != tc.code || len(e.argv) != 0) {
			t.Fatalf("%s: %v, exec %q", name, err, e.argv)
		}
	}
}
