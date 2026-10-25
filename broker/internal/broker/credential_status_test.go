package broker

import (
	"bytes"
	"testing"
)

func TestClaudeCredentialUsable(t *testing.T) {
	for name, tc := range map[string]struct {
		k    *fakeKeychain
		want bool
	}{
		"ready":        {k: &fakeKeychain{token: []byte("sk-ant-oat01-fixture")}, want: true},
		"missing":      {k: &fakeKeychain{fakeApps: fakeApps{accessErr: ErrNotFound}}},
		"other broker": {k: &fakeKeychain{fakeApps: fakeApps{access: reinstalled()}, token: []byte("fixture")}},
		"locked":       {k: &fakeKeychain{getErr: ErrNeedsDialog}},
		"empty":        {k: &fakeKeychain{}},
		"invalid":      {k: &fakeKeychain{token: []byte("has whitespace")}},
	} {
		t.Run(name, func(t *testing.T) {
			if got := (Credentials{Keychain: tc.k}).Usable("default"); got != tc.want {
				t.Fatalf("usable=%v, want %v", got, tc.want)
			}
			if tc.k.dialogGets != 0 || len(tc.k.steps) != 0 || len(tc.k.put) != 0 {
				t.Fatal("status prompted or changed the item")
			}
			for _, b := range tc.k.returned {
				if !bytes.Equal(b, make([]byte, len(b))) {
					t.Fatal("status did not erase its read buffer")
				}
			}
		})
	}
}
