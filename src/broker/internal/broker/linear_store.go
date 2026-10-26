package broker

import (
	"bytes"
	"errors"
	"slices"
	"strings"
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

// previousBrokerOnly recognizes a record restricted to one prior code identity at the installed broker path.
// A permissive or foreign application's record is never adopted by interactive re-enrollment.
func previousBrokerOnly(a AppAccess) bool {
	previous, partition := "", ""
	for _, entry := range a.Entries {
		if slices.ContainsFunc(entry.Authorizations, func(auth string) bool { return slices.Contains(readAuthorizations, auth) }) {
			if entry.Any || len(entry.Apps) != 1 {
				return false
			}
			app := entry.Apps[0]
			if !strings.HasPrefix(app, InstalledPath+" (") || !strings.HasSuffix(app, ")") || previous != "" && previous != app {
				return false
			}
			previous = app
		}
		for _, auth := range entry.Authorizations {
			if auth == "partition_id" {
				if partition != "" || len(entry.Partitions) != 1 {
					return false
				}
				partition = entry.Partitions[0]
			}
		}
	}
	if previous == "" || !(strings.HasPrefix(partition, "cdhash:") || strings.HasPrefix(partition, "teamid:")) {
		return false
	}
	a.Broker, a.Partition = previous, partition
	return onlyBroker(a) == nil
}

func (l Linear) repairMetadata(service, account string) error {
	a, err := l.keys.AppAccess(service, account)
	if errors.Is(err, ErrNotFound) {
		return nil
	}
	if err != nil {
		return errLinear
	}
	if onlyBroker(a) == nil {
		return nil
	}
	if !previousBrokerOnly(a) {
		return errLinear
	}
	unlock, err := l.lock("trust/" + service + "/" + account)()
	if err != nil {
		return errLinear
	}
	defer unlock()
	a, err = l.keys.AppAccess(service, account)
	if err != nil {
		return errLinear
	}
	if onlyBroker(a) == nil {
		return nil
	}
	if !previousBrokerOnly(a) {
		return errLinear
	}
	if secureItem(service, account, "rerun login linear", l.keys, l.password) != nil {
		return errLinear
	}
	return nil
}
