package broker

import (
	"bytes"
	"errors"
	"reflect"
	"slices"
	"strings"
	"testing"

	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/launcher"
	"ensemble/broker/internal/protocol"
)

const envValue = "mini-PASSWORD with spaces"

// loginEnv runs LoginEnv for dummy with value; it fails t when the value or password reaches an error or output, or
// a buffer stays unerased.
func loginEnv(t *testing.T, k *fakeSecrets, ask *askOnce, value string) (string, error) {
	t.Helper()
	var out bytes.Buffer
	read := []byte(value)
	err := LoginEnv("dummy", func() ([]byte, error) { return read, nil }, ask.read, k, &out)
	text := out.String()
	if err != nil {
		text += err.Error()
	}
	if value != "" && strings.Contains(text, value) || strings.Contains(text, keychainPass) {
		t.Fatalf("secret in %q", text)
	}
	if !bytes.Equal(read, make([]byte, len(read))) {
		t.Fatal("value buffer not erased")
	}
	for _, b := range k.returned {
		if !bytes.Equal(b, make([]byte, len(b))) {
			t.Fatal("read buffer not erased")
		}
	}
	return out.String(), err
}

func TestLoginEnv(t *testing.T) {
	k := &fakeSecrets{}
	out, err := loginEnv(t, k, &askOnce{}, envValue)
	if err != nil || out != "Environment secret ensemble-env/dummy saved; keychain trusts only the broker\n" {
		t.Fatalf("%v %q", err, out)
	}
	if string(k.items["ensemble-env/dummy/dummy"]) != envValue || !slices.Equal(k.puts, []string{"ensemble-env/dummy/dummy"}) {
		t.Fatalf("%q %q", k.items, k.puts)
	}
	access, err := k.AppAccess("ensemble-env/dummy", "dummy")
	if err != nil || onlyBroker(access) != nil {
		t.Fatalf("access %v %v", onlyBroker(access), err)
	}
}

// The item PutSecret makes still needs its partition list: secureItem asks the password once, then the read proves it.
func TestLoginEnvTrustsOnlyBroker(t *testing.T) {
	k := &fakeSecrets{fakeApps: fakeApps{access: halfDone()}}
	ask := &askOnce{}
	if _, err := loginEnv(t, k, ask, envValue); err != nil || ask.calls != 1 || !slices.Equal(k.steps, []string{"partition ensemble-env/dummy dummy"}) {
		t.Fatalf("%v: asked %d, steps %q", err, ask.calls, k.steps)
	}
	if access, _ := k.AppAccess("ensemble-env/dummy", "dummy"); onlyBroker(access) != nil {
		t.Fatal("item still trusts another application")
	}
	k = &fakeSecrets{fakeApps: fakeApps{access: halfDone(), partitionErr: errors.New("keychain status -25293")}}
	_, err := loginEnv(t, k, &askOnce{}, envValue)
	if err == nil || !strings.Contains(err.Error(), "keychain item ensemble-env/dummy") || !strings.HasSuffix(err.Error(), "rerun ensemble-broker login env dummy to finish") {
		t.Fatalf("refused: %v", err)
	}
}

func TestLoginEnvValueLimits(t *testing.T) {
	for _, value := range []string{"-----BEGIN KEY-----\nline\n-----END KEY-----\n", "x", "\x00\t ", strings.Repeat("v", launcher.MaxSecretBytes)} {
		k := &fakeSecrets{}
		if _, err := loginEnv(t, k, &askOnce{}, value); err != nil || string(k.items["ensemble-env/dummy/dummy"]) != value {
			t.Fatalf("%.20q: %v", value, err)
		}
	}
	for _, value := range []string{"", strings.Repeat("v", launcher.MaxSecretBytes+1)} {
		k := &fakeSecrets{}
		ask := &askOnce{}
		if _, err := loginEnv(t, k, ask, value); err == nil || err.Error() != "the value must be 1 to 16384 bytes" || len(k.puts) != 0 || ask.calls != 0 {
			t.Fatalf("%d bytes: %v", len(value), err)
		}
	}
	var out bytes.Buffer
	err := LoginEnv("dummy", func() ([]byte, error) { return nil, errors.New("needs an interactive terminal") }, (&askOnce{}).read, &fakeSecrets{}, &out)
	if err == nil || err.Error() != "reading the value failed: needs an interactive terminal" {
		t.Fatalf("read error: %v", err)
	}
}

func TestLoginEnvRefusesName(t *testing.T) {
	for _, bad := range []string{"", "Dummy", "-x", ".x", "a/b", "a b", strings.Repeat("p", 65)} {
		k := &fakeSecrets{}
		var out bytes.Buffer
		if err := LoginEnv(bad, func() ([]byte, error) { return []byte(envValue), nil }, (&askOnce{}).read, k, &out); err == nil || len(k.puts) != 0 {
			t.Fatalf("%q accepted: %v", bad, err)
		}
	}
}

func TestLoginEnvReadsBack(t *testing.T) {
	for name, tc := range map[string]struct {
		k     *fakeSecrets
		fails string
	}{
		"other bytes": {&fakeSecrets{readBack: []byte("other")},
			"keychain item ensemble-env/dummy: reading it failed: it holds other bytes; rerun ensemble-broker login env dummy"},
		"needs dialog": {&fakeSecrets{readErr: ErrNeedsDialog},
			"keychain item ensemble-env/dummy: reading it failed: keychain status -25308; rerun ensemble-broker login env dummy"},
		"put fails": {&fakeSecrets{putErr: errors.New("keychain status -25299")},
			"keychain item ensemble-env/dummy: saving it failed: keychain status -25299; rerun ensemble-broker login env dummy"},
	} {
		if _, err := loginEnv(t, tc.k, &askOnce{}, envValue); err == nil || err.Error() != tc.fails {
			t.Fatalf("%s: %v", name, err)
		}
	}
}

// TestEnvPrefixMatchesLauncher: the launcher's reference rule and this package's service prefix agree.
func TestEnvPrefixMatchesLauncher(t *testing.T) {
	if !launcher.ValidEnvItem(envServicePrefix+"x") || launcher.ValidEnvItem("other/x") || launcher.ValidEnvItem(envServicePrefix) {
		t.Fatal("launcher.ValidEnvItem disagrees with envServicePrefix")
	}
}

// envKeys is a keychain holding each named environment secret, its value envValue-<name>.
func envKeys(names ...string) *fakeSecrets {
	k := &fakeSecrets{items: map[string][]byte{}}
	for _, n := range names {
		k.items["ensemble-env/"+n+"/"+n] = []byte(envValue + "-" + n)
	}
	return k
}

func TestEnvKeysReady(t *testing.T) {
	login := func(name string) string { return "run ensemble-broker login env " + name + "." }
	for name, tc := range map[string]struct {
		k       *fakeSecrets
		code    protocol.Code
		message string
	}{
		"all":     {k: envKeys("dummy", "ssh")},
		"missing": {k: envKeys("dummy"), code: protocol.LoginRequired, message: "No keychain item ensemble-env/ssh: " + login("ssh")},
		"other build": {k: func() *fakeSecrets { k := envKeys("dummy", "ssh"); k.access = reinstalled(); return k }(), code: protocol.LoginRequired,
			message: "The keychain item ensemble-env/dummy trusts another broker build: " + login("dummy")},
		"needs dialog": {k: func() *fakeSecrets { k := envKeys("dummy", "ssh"); k.readErr = ErrNeedsDialog; return k }(), code: protocol.KeychainFailed,
			message: "The keychain item ensemble-env/dummy needs keychain approval: unlock the login keychain, or " + login("dummy")},
		"access fails": {k: func() *fakeSecrets { k := envKeys("dummy", "ssh"); k.accessErr = errors.New("user denied"); return k }(),
			code: protocol.KeychainFailed, message: "Keychain access to ensemble-env/dummy failed."},
		"read fails": {k: func() *fakeSecrets {
			k := envKeys("dummy", "ssh")
			k.readErr = errors.New("keychain status -25293")
			return k
		}(),
			code: protocol.KeychainFailed, message: "Keychain access to ensemble-env/dummy failed."},
	} {
		err := EnvKeys{Keychain: tc.k}.Ready([]string{"ensemble-env/dummy", "ensemble-env/ssh"})
		var e *protocol.Error
		if tc.code == "" && err != nil || tc.code != "" && (!errors.As(err, &e) || e.Code != tc.code || e.Message != tc.message || e.Cause != "") {
			t.Fatalf("%s: %v", name, err)
		}
		if err != nil && strings.Contains(err.Error(), envValue) {
			t.Fatalf("%s: value in %v", name, err)
		}
		for _, b := range tc.k.returned {
			if !bytes.Equal(b, make([]byte, len(b))) {
				t.Fatalf("%s: read buffer not erased", name)
			}
		}
	}
	if err := (EnvKeys{Keychain: envKeys()}).Ready([]string{"ensemble-bot"}); err == nil {
		t.Fatal("a reference outside ensemble-env/ was read")
	}
}

func TestEnvKeysInject(t *testing.T) {
	e := &fakeExec{}
	k := envKeys("dummy")
	if err := (EnvKeys{Keychain: k}).Inject(launcher.Secret{Item: "ensemble-env/dummy", File: "dummy.txt"}, e.run); err != nil {
		t.Fatal(err)
	}
	script := "mkdir -p -m 0700 " + docker.SecretsDir + "/env && umask 0377 && set -C && cat > " + docker.SecretsDir + `/env/"$1"`
	if want := [][]string{{"/bin/sh", "-c", script, "sh", "dummy.txt"}}; !reflect.DeepEqual(e.argv, want) || string(e.stdin[0]) != envValue+"-dummy" {
		t.Fatalf("%q %q", e.argv, e.stdin)
	}
	if !slices.Equal(k.reads, []string{"ensemble-env/dummy/dummy"}) {
		t.Fatalf("reads %q", k.reads)
	}
	for _, b := range k.returned {
		if !bytes.Equal(b, make([]byte, len(b))) {
			t.Fatal("read buffer not erased")
		}
	}
	secret := launcher.Secret{Item: "ensemble-env/dummy", File: "dummy.txt"}
	for name, tc := range map[string]struct {
		k           *fakeSecrets
		dockerErr   error
		code        protocol.Code
		message     string
		unreachable bool
	}{
		"docker fails": {k: envKeys("dummy"), dockerErr: &docker.CommandError{Exit: 1, Stderr: "cat: " + envValue}, code: protocol.StartFailed,
			message: "Writing ensemble-env/dummy into the container failed."},
		"unreachable":  {k: envKeys("dummy"), dockerErr: errors.Join(docker.ErrUnreachable, errors.New(envValue)), unreachable: true},
		"missing":      {k: envKeys(), code: protocol.LoginRequired, message: "No keychain item ensemble-env/dummy: run ensemble-broker login env dummy."},
		"needs dialog": {k: func() *fakeSecrets { k := envKeys("dummy"); k.readErr = ErrNeedsDialog; return k }(), code: protocol.KeychainFailed},
	} {
		e := &fakeExec{err: tc.dockerErr}
		err := EnvKeys{Keychain: tc.k}.Inject(secret, e.run)
		var pe *protocol.Error
		var ce *docker.CommandError
		if err == nil || strings.Contains(err.Error(), envValue) || errors.Is(err, docker.ErrUnreachable) != tc.unreachable || errors.As(err, &ce) {
			t.Fatalf("%s: %v", name, err)
		}
		if tc.code != "" && (!errors.As(err, &pe) || pe.Code != tc.code || tc.message != "" && pe.Message != tc.message) {
			t.Fatalf("%s: %v", name, err)
		}
		if tc.dockerErr == nil && len(e.argv) != 0 {
			t.Fatalf("%s: exec %q", name, e.argv)
		}
	}
}
