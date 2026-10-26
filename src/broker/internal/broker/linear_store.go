package broker

import (
	"bytes"
	"errors"
)

type retainedSecretStore interface {
	AppAccess(string, string) (AppAccess, error)
	CreateSecret(string, string, []byte) error
	WriteSecret(string, string, []byte) error
	Secret(string, string) ([]byte, error)
}

// writeRetainedSecret never deletes an old value. Initial creation adds the complete data without replacement;
// an existing value survives an unsuccessful SecItemUpdate. The caller serializes writers.
func writeRetainedSecret(k retainedSecretStore, service, account string, data []byte) error {
	if len(data) == 0 {
		return errLinear
	}
	a, err := k.AppAccess(service, account)
	created := false
	if errors.Is(err, ErrNotFound) {
		if err = k.CreateSecret(service, account, data); err != nil {
			return err
		}
		created = true
		a, err = k.AppAccess(service, account)
	}
	if err != nil {
		return err
	}
	if err = onlyBroker(a); err != nil {
		return err
	}
	if !created {
		if err = k.WriteSecret(service, account, data); err != nil {
			return err
		}
	}
	stored, err := k.Secret(service, account)
	defer erase(stored)
	if err != nil {
		return err
	}
	if !bytes.Equal(stored, data) {
		return errLinear
	}
	return nil
}

// linearStore authenticates account/registry/grant records and retains old durable values on failed updates.
type linearStore struct{ linearKeys }

func (s linearStore) Secret(service, account string) ([]byte, error) {
	access, err := s.AppAccess(service, account)
	if err != nil {
		return nil, err
	}
	if onlyBroker(access) != nil {
		return nil, errLinear
	}
	return s.linearKeys.Secret(service, account)
}

func (s linearStore) PutSecret(service, account string, data []byte) error {
	return s.UpdateSecret(service, account, data)
}
