package credential

import (
	"errors"
	"reflect"
	"strings"
	"testing"

	"ensemble/broker/internal/docker"
)

type recorded struct {
	argv  [][]string
	stdin [][]byte
	err   error
}

func (r *recorded) exec(argv []string, stdin []byte) error {
	r.argv, r.stdin = append(r.argv, argv), append(r.stdin, append([]byte(nil), stdin...))
	return r.err
}

func TestDeliverScripts(t *testing.T) {
	dir := docker.SecretsDir + "/github"
	for _, tc := range []struct {
		file   File
		script string
	}{
		{File{dir, "acme", false}, "mkdir -p -m 0700 " + dir + ` && umask 0377 && set -C && cat > ` + dir + `/"$1"`},
		{File{dir, "acme", true}, "mkdir -p -m 0700 " + dir + ` && umask 0377 && rm -f ` + dir + `/."$1".new && cat > ` + dir +
			`/."$1".new && mv -f ` + dir + `/."$1".new ` + dir + `/"$1"`},
	} {
		r := &recorded{}
		if err := Deliver(r.exec, tc.file, []byte("ghs_secret")); err != nil {
			t.Fatal(err)
		}
		if want := [][]string{{"/bin/sh", "-c", tc.script, "sh", "acme"}}; !reflect.DeepEqual(r.argv, want) {
			t.Fatalf("argv %q", r.argv)
		}
		if string(r.stdin[0]) != "ghs_secret" {
			t.Fatalf("stdin %q", r.stdin[0])
		}
	}
}

func TestDeliverRejectsNames(t *testing.T) {
	for _, f := range []File{{docker.SecretsDir, "", false}, {docker.SecretsDir, "a/b", false}, {docker.SecretsDir, "../claude", false},
		{docker.SecretsDir, ".", false}, {docker.SecretsDir, "..", false}, {docker.SecretsDir, ".acme", true}} {
		r := &recorded{}
		if err := Deliver(r.exec, f, []byte("x")); !errors.Is(err, ErrDelivery) || len(r.argv) != 0 {
			t.Errorf("%+v: %v, exec %q", f, err, r.argv)
		}
	}
	r := &recorded{}
	if err := Deliver(r.exec, File{docker.SecretsDir + "/env", ".npmrc", false}, []byte("x")); err != nil || len(r.argv) != 1 {
		t.Fatalf("an env secret's dot file: %v", err)
	}
}

func TestDeliverErrors(t *testing.T) {
	const secret = "ghs_sentinel"
	for _, tc := range []struct {
		err         error
		unreachable bool
	}{
		{&docker.CommandError{Exit: 1, Stderr: "cat: " + secret}, false},
		{errors.Join(docker.ErrUnreachable, errors.New(secret)), true},
	} {
		r := &recorded{err: tc.err}
		err := Deliver(r.exec, File{docker.SecretsDir, "claude", false}, []byte(secret))
		if err == nil || strings.Contains(err.Error(), secret) || errors.Is(err, docker.ErrUnreachable) != tc.unreachable ||
			!tc.unreachable && !errors.Is(err, ErrDelivery) {
			t.Fatalf("%v", err)
		}
		var ce *docker.CommandError
		if errors.As(err, &ce) {
			t.Fatal("Docker stderr kept")
		}
	}
}
