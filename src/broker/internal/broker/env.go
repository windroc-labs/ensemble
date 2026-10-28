package broker

import (
	"errors"
	"fmt"
	"io"
	"strings"

	"ensemble/broker/internal/credential"
	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/launcher"
	"ensemble/broker/internal/protocol"
)

// An environment secret: service envServicePrefix+<name>, account <name> (launcher.ValidEnvName); in a container, the
// file envDir/<file> a start manifest names.
const (
	envServicePrefix = "ensemble-broker-env/"
	envDir           = docker.EnvDir
)

// envLogin is the command that stores environment secret name.
func envLogin(name string) string {
	return credential.LoginCommand(credential.StaticRef(credential.Env, name))
}

// EnvKeychain holds the environment secrets as broker-only items.
type EnvKeychain interface{ brokerOnlyStore }

// LoginEnv reads name's value with read, stores it in a broker-only item (replacing an earlier one), makes the item
// trust only the broker (secureItem, asking password when needed) and proves start's no-dialog read returns the value.
// The value is any 1 to launcher.MaxSecretBytes bytes. Neither out nor an error sees it.
func LoginEnv(name string, read, password func() ([]byte, error), k EnvKeychain, out io.Writer) error {
	if !launcher.ValidEnvName(name) {
		return fmt.Errorf("invalid environment secret name %q", name)
	}
	value, err := read()
	defer erase(value)
	if err != nil {
		return fmt.Errorf("reading the value failed: %w", err)
	}
	if len(value) == 0 || len(value) > launcher.MaxSecretBytes {
		return fmt.Errorf("the value must be 1 to %d bytes", launcher.MaxSecretBytes)
	}
	service := envServicePrefix + name
	if err := storeBrokerOnly(service, name, "rerun "+envLogin(name), value, k, password); err != nil {
		return err
	}
	_, err = fmt.Fprintf(out, "Environment secret %s saved; keychain trusts only the broker\n", service)
	return err
}

// EnvKeys is start's side of the environment secrets: each reference is ensemble-broker-env/<name>.
type EnvKeys struct{ Keychain EnvKeychain }

// envError maps a keychain failure for ref's item to the protocol, naming ref, without its detail.
func envError(ref, name string, err error) *protocol.Error {
	login := "run " + envLogin(name) + "."
	switch {
	case errors.Is(err, ErrNotFound):
		return protocol.New(protocol.LoginRequired, "No keychain item "+ref+": "+login)
	case errors.Is(err, ErrNeedsDialog):
		return protocol.New(protocol.KeychainFailed, "The keychain item "+ref+" needs keychain approval: unlock the login keychain, or "+login)
	}
	return protocol.New(protocol.KeychainFailed, "Keychain access to "+ref+" failed.")
}

// envName is ref's name, ref being ensemble-broker-env/<name>; the launcher already refused any other reference.
func envName(ref string) (string, error) {
	if !launcher.ValidEnvItem(ref) {
		return "", protocol.Newf(protocol.ManifestRejected, "Manifest rejected: invalid secret item %q", ref)
	}
	return strings.TrimPrefix(ref, envServicePrefix), nil
}

// read is ref's value, read without a dialog; the caller erases it.
func (e EnvKeys) read(ref string) ([]byte, error) {
	name, err := envName(ref)
	if err != nil {
		return nil, err
	}
	value, err := readSecret(e.Keychain, ref, name)
	if err != nil {
		return nil, envError(ref, name, err)
	}
	return value, nil
}

// Ready checks each item's access, then reads it once without a dialog, so an item a start could not read fails
// before any container exists.
func (e EnvKeys) Ready(refs []string) error {
	for _, ref := range refs {
		name, err := envName(ref)
		if err != nil {
			return err
		}
		trusted, earlier, err := checkItem(e.Keychain, ref, name)
		switch {
		case err != nil:
			return envError(ref, name, err)
		case earlier:
			return protocol.New(protocol.LoginRequired, "The keychain item "+ref+" trusts an earlier broker build: run "+RetrustCommand+".")
		case !trusted:
			return protocol.New(protocol.LoginRequired, "The keychain item "+ref+" trusts another broker build: run "+envLogin(name)+".")
		}
		value, err := e.read(ref)
		erase(value)
		if err != nil {
			return err
		}
	}
	return nil
}

// Inject prepares s's write; the caller owns and erases the command.
func (e EnvKeys) Inject(s launcher.Secret) (credential.Command, error) {
	value, err := e.read(s.Item)
	defer erase(value)
	if err != nil {
		return credential.Command{}, err
	}
	return credential.Prepare(credential.File{Dir: envDir, Name: s.File}, value)
}
