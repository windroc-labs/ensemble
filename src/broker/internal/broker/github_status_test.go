package broker

import (
	"encoding/json"
	"slices"
	"strings"
	"testing"

	"ensemble/broker/internal/protocol"
)

type statusSecrets struct {
	*secrets
	t *testing.T
}

func (s statusSecrets) PutSecret(string, string, []byte) error {
	s.t.Fatal("status wrote credentials")
	return nil
}

func (s statusSecrets) DeleteSecret(string, string) error {
	s.t.Fatal("status deleted credentials")
	return nil
}

func TestPendingReadOnly(t *testing.T) {
	list := []pending{{ctr, "acme", "ghs_STATUS_SENTINEL", "2000-01-01T00:00:00Z"}}
	data, _ := json.Marshal(list)
	keys := &secrets{items: map[string][]byte{revocationsService + "/" + revocationsAccount: data}}
	g := GitHub{Keychain: statusSecrets{keys, t}, Lock: func() (func(), error) {
		t.Fatal("status acquired a mutating lock")
		return nil, nil
	}}
	for range 2 {
		got, err := g.Pending()
		want := []protocol.Pending{{Container: ctr, Account: "acme", ExpiresAt: list[0].ExpiresAt}}
		if err != nil || !slices.Equal(got, want) {
			t.Fatalf("%+v %v", got, err)
		}
		encoded, _ := json.Marshal(got)
		if strings.Contains(string(encoded), "ghs_STATUS_SENTINEL") || !slices.Equal(keys.items[revocationsService+"/"+revocationsAccount], data) {
			t.Fatal("status exposed or changed a token")
		}
	}
}

func TestPendingEmptyAndReadFailures(t *testing.T) {
	for _, mode := range []string{"empty", "unreadable", "malformed"} {
		t.Run(mode, func(t *testing.T) {
			keys := &secrets{items: map[string][]byte{}, unreadable: map[string]bool{revocationsService: mode == "unreadable"}}
			if mode == "malformed" {
				keys.items[revocationsService+"/"+revocationsAccount] = []byte("STATUS_SECRET_NOT_JSON")
			}
			got, err := (GitHub{Keychain: statusSecrets{keys, t}}).Pending()
			if mode == "empty" {
				encoded, _ := json.Marshal(got)
				if err != nil || string(encoded) != "[]" {
					t.Fatalf("%s %v", encoded, err)
				}
			} else if err == nil || !strings.Contains(err.Error(), string(protocol.KeychainFailed)) || strings.Contains(err.Error(), "STATUS_SECRET") {
				t.Fatalf("unsafe or missing read error: %v", err)
			}
		})
	}
}
