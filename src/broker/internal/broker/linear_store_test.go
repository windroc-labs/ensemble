package broker

import (
	"bytes"
	"errors"
	"testing"
)

type retainedFake struct {
	value                                     []byte
	failCreate, failWrite, badRead, untrusted bool
	creates, writes                           int
}

func (k *retainedFake) AppAccess(string, string) (AppAccess, error) {
	if k.value == nil {
		return AppAccess{}, ErrNotFound
	}
	if k.untrusted {
		return *added(), nil
	}
	return brokerOnly(), nil
}
func (k *retainedFake) CreateSecret(_, _ string, v []byte) error {
	k.creates++
	if k.failCreate {
		return errors.New("create failed")
	}
	k.value = bytes.Clone(v)
	return nil
}
func (k *retainedFake) WriteSecret(_, _ string, v []byte) error {
	k.writes++
	if k.failWrite {
		return errors.New("update failed")
	}
	k.value = bytes.Clone(v)
	return nil
}
func (k *retainedFake) Secret(string, string) ([]byte, error) {
	if k.badRead {
		return []byte("other"), nil
	}
	return bytes.Clone(k.value), nil
}

func TestLinearRetainedStoreUpdatesWithoutDeletingOldData(t *testing.T) {
	k := &retainedFake{value: []byte("old"), failWrite: true}
	if writeRetainedSecret(k, "service", "account", []byte("new")) == nil || string(k.value) != "old" || k.creates != 0 {
		t.Fatal("failed update lost old value")
	}
	k.failWrite = false
	if err := writeRetainedSecret(k, "service", "account", []byte("new")); err != nil || string(k.value) != "new" {
		t.Fatalf("update %v", err)
	}
	for _, failure := range []string{"create", "readback", "trust"} {
		t.Run(failure, func(t *testing.T) {
			store := &retainedFake{}
			switch failure {
			case "create":
				store.failCreate = true
			case "readback":
				store.badRead = true
			case "trust":
				store.value = []byte("old")
				store.untrusted = true
			}
			if writeRetainedSecret(store, "service", "account", []byte("new")) == nil {
				t.Fatal("unconfirmed write accepted")
			}
			if failure == "create" && store.value != nil {
				t.Fatal("failed create left invalid placeholder")
			}
			if failure == "trust" && store.writes != 0 {
				t.Fatal("permissive item overwritten")
			}
		})
	}
	k = &retainedFake{}
	if err := writeRetainedSecret(k, "service", "account", []byte("new")); err != nil || k.creates != 1 || k.writes != 0 {
		t.Fatalf("initial complete creation %v", err)
	}
}
