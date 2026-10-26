package credential

import (
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
)

const storeSentinel = "SENTINEL-c0ffee"

func tempStore(t *testing.T) StaticStore {
	t.Helper()
	return StaticStore{Path: filepath.Join(t.TempDir(), "docker", "static-secrets.json")}
}

func loaded(t *testing.T, s StaticStore) map[string]Static {
	t.Helper()
	m, err := s.Load()
	if err != nil {
		t.Fatal(err)
	}
	return m
}

func TestStoreAbsentIsEmpty(t *testing.T) {
	if m := loaded(t, tempStore(t)); len(m) != 0 {
		t.Fatalf("%v", m)
	}
}

func TestStoreErrors(t *testing.T) {
	s := tempStore(t)
	if err := os.MkdirAll(filepath.Dir(s.Path), 0o700); err != nil {
		t.Fatal(err)
	}
	for _, bad := range []string{
		"not json " + storeSentinel,
		`{"secrets": {"ensemble-claude/default": {"version": -1, "provenance": "unknown"}}}`,
		`{"secrets": {"ensemble-claude/default": {"version": 1, "provenance": "guessed"}}}`,
		`{"secrets": {"ensemble-claude/default": {"version": 1, "provenance": "explicit", "expires_at": "soon"}}}`,
		`{"secrets": {"ensemble-claude/default": {"version": 1, "provenance": "explicit"}}}`,
		`{"secrets": {"other/x": {"version": 1, "provenance": "unknown"}}}`,
		`{"secrets": {}, "extra": 1}`,
	} {
		if err := os.WriteFile(s.Path, []byte(bad), 0o600); err != nil {
			t.Fatal(err)
		}
		_, err := s.Load()
		if err == nil || err.Error() != s.Path+": invalid content" {
			t.Errorf("%q: %v", bad, err)
		}
	}
	if err := os.Remove(s.Path); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(s.Path, 0o700); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Load(); err == nil || err.Error() != s.Path+": cannot read" {
		t.Errorf("a directory: %v", err)
	}
	locked := tempStore(t)
	locked.Lock = func() (func(), error) { return nil, errors.New("flock " + storeSentinel) }
	_, err := locked.Register("ensemble-env/db", Registration{}, staticNow, func() error { return nil })
	if err == nil || err.Error() != locked.Path+": cannot lock" {
		t.Errorf("lock: %v", err)
	}
}

func TestRegister(t *testing.T) {
	s := tempStore(t)
	var during map[string]Static
	got, err := s.Register("ensemble-claude/default", Registration{Fresh: true}, staticNow, func() error {
		during = loaded(t, s)
		return nil
	})
	want := Static{Version: 1, ExpiresAt: "2027-10-26T12:00:00Z", Provenance: Assumed}
	if err != nil || !reflect.DeepEqual(got, want) {
		t.Fatalf("%+v %v", got, err)
	}
	if p := during["ensemble-claude/default"]; !p.Pending || p.Version != 1 || p.Expired(staticNow.AddDate(5, 0, 0)) {
		t.Fatalf("during the login: %+v", p)
	}
	if m := loaded(t, s); !reflect.DeepEqual(m["ensemble-claude/default"], want) {
		t.Fatalf("%+v", m)
	}
	got, err = s.Register("ensemble-claude/default", Registration{}, staticNow, func() error { return nil })
	if err != nil || !reflect.DeepEqual(got, Static{Version: 2, Provenance: Unknown}) {
		t.Fatalf("second: %+v %v", got, err)
	}
	for _, path := range []string{s.Path, filepath.Dir(s.Path)} {
		info, err := os.Stat(path)
		if err != nil {
			t.Fatal(err)
		}
		if mode := info.Mode().Perm(); path == s.Path && mode != 0o600 || path != s.Path && mode != 0o700 {
			t.Errorf("%s: %v", path, mode)
		}
	}
}

func TestRegisterBadRegistrationStopsFirst(t *testing.T) {
	s := tempStore(t)
	called := false
	_, err := s.Register("ensemble-env/db", Registration{Fresh: true}, staticNow, func() error { called = true; return nil })
	if err == nil || called {
		t.Fatalf("%v %v", err, called)
	}
	if _, statErr := os.Stat(s.Path); !os.IsNotExist(statErr) {
		t.Fatalf("store written: %v", statErr)
	}
}

func TestRegisterStoreFailsRestores(t *testing.T) {
	s := tempStore(t)
	prior, err := s.Register("ensemble-env/db", Registration{Expires: date(t, "2027-01-01")}, staticNow, func() error { return nil })
	if err != nil {
		t.Fatal(err)
	}
	keychain := errors.New("keychain status -25293")
	_, err = s.Register("ensemble-env/db", Registration{}, staticNow, func() error { return keychain })
	if !errors.Is(err, keychain) {
		t.Fatalf("%v", err)
	}
	if m := loaded(t, s); !reflect.DeepEqual(m["ensemble-env/db"], prior) {
		t.Fatalf("not restored: %+v", m)
	}
	_, err = s.Register("ensemble-env/new", Registration{}, staticNow, func() error { return keychain })
	if !errors.Is(err, keychain) {
		t.Fatal(err)
	}
	if _, ok := loaded(t, s)["ensemble-env/new"]; ok {
		t.Fatal("a failed first login left a record")
	}
}

func TestRegisterCommitFailsStaysPending(t *testing.T) {
	s := tempStore(t)
	_, err := s.Register("ensemble-env/db", Registration{Expires: date(t, "2027-01-01")}, staticNow, func() error {
		// The directory turns unwritable once the login began, so only the commit fails.
		return os.Chmod(filepath.Dir(s.Path), 0o500)
	})
	defer os.Chmod(filepath.Dir(s.Path), 0o700)
	want := "ensemble-env/db saved; recording its expiry in " + s.Path + " failed (cannot write); rerun the login"
	if err == nil || err.Error() != want {
		t.Fatalf("%v", err)
	}
	if os.Getuid() == 0 {
		t.Skip("root writes anyway")
	}
	if p := loaded(t, s)["ensemble-env/db"]; !p.Pending || p.Expired(staticNow.AddDate(5, 0, 0)) {
		t.Fatalf("%+v", p)
	}
}

func TestRegisterNewerLoginWins(t *testing.T) {
	s := tempStore(t)
	var inner Static
	got, err := s.Register("ensemble-env/db", Registration{Expires: date(t, "2027-01-01")}, staticNow, func() error {
		var err error
		inner, err = s.Register("ensemble-env/db", Registration{Expires: date(t, "2027-06-01")}, staticNow, func() error { return nil })
		return err
	})
	if err != nil || inner.Version != 2 || !reflect.DeepEqual(got, inner) {
		t.Fatalf("a superseded login returns the newer record: %+v %+v %v", got, inner, err)
	}
	if m := loaded(t, s); !reflect.DeepEqual(m["ensemble-env/db"], inner) {
		t.Fatalf("the older commit overwrote the newer login: %+v", m)
	}
	_, err = s.Register("ensemble-env/db", Registration{}, staticNow, func() error {
		if _, err := s.Register("ensemble-env/db", Registration{}, staticNow, func() error { return nil }); err != nil {
			return err
		}
		return errors.New("keychain status -25293")
	})
	if err == nil {
		t.Fatal("no error")
	}
	if m := loaded(t, s); m["ensemble-env/db"].Version != 4 || m["ensemble-env/db"].Pending {
		t.Fatalf("the older restore undid the newer login: %+v", m)
	}
}

func TestDeliverHost(t *testing.T) {
	s := tempStore(t)
	if _, err := s.Register("ensemble-env/db", Registration{Expires: date(t, "2026-11-20")}, staticNow, func() error { return nil }); err != nil {
		t.Fatal(err)
	}
	var told []Notice
	emit := func(n Notice) { told = append(told, n) }
	for range 2 {
		if err := s.DeliverHost("ensemble-env/db", staticNow, emit); err != nil {
			t.Fatal(err)
		}
	}
	if len(told) != 1 || told[0].Key != "ensemble-env/db@1:30d" || !strings.HasPrefix(told[0].Text, "ensemble-env/db expires 2026-11-20") {
		t.Fatalf("%+v", told)
	}
	if err := s.DeliverHost("ensemble-env/db", time.Date(2026, 11, 14, 0, 0, 0, 0, time.UTC), emit); err != nil || len(told) != 2 || told[1].Key != "ensemble-env/db@1:7d" {
		t.Fatalf("%+v %v", told, err)
	}
	if err := s.DeliverHost("ensemble-env/absent", staticNow, emit); err != nil || len(told) != 2 {
		t.Fatalf("absent: %v", err)
	}
	if got := loaded(t, s)["ensemble-env/db"].Notified; !reflect.DeepEqual(got, []string{"30d", "7d"}) {
		t.Fatalf("%q", got)
	}
}

func TestRegisterNeverRestoresAnotherLoginPending(t *testing.T) {
	s := tempStore(t)
	got, err := s.Register("ensemble-env/db", Registration{Expires: date(t, "2027-01-01")}, staticNow, func() error {
		_, err := s.Register("ensemble-env/db", Registration{}, staticNow, func() error { return errors.New("keychain status -25293") })
		if err == nil {
			t.Fatal("inner login did not fail")
		}
		return nil
	})
	want := Static{Version: 1, Provenance: Unknown}
	if err != nil || !reflect.DeepEqual(got, want) {
		t.Fatalf("%+v %v", got, err)
	}
	if r := loaded(t, s)["ensemble-env/db"]; !reflect.DeepEqual(r, want) {
		t.Fatalf("stuck: %+v", r)
	}
}
