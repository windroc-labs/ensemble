package broker

import (
	"errors"
	"fmt"
	"io"

	"ensemble/broker/internal/credential"
	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/launcher"
	"ensemble/broker/internal/protocol"
)

// An OpenCode provider's API key: service opencodeService, account the provider's name; in a container, the file
// opencodeKeyDir/<provider>.key.
const (
	opencodeService = "ensemble-broker-opencode"
	opencodeKeyDir  = docker.SecretsDir + "/opencode"
)

// opencodeLogin is the command that stores provider's API key.
func opencodeLogin(provider string) string {
	return credential.LoginCommand(credential.StaticRef(credential.Opencode, provider))
}

// OpencodeKeychain holds the providers' API keys as broker-only items.
type OpencodeKeychain interface{ brokerOnlyStore }

// LoginOpencode reads provider's API key with read, checks it, stores it in a broker-only item (replacing an earlier
// one), makes the item trust only the broker (secureItem, asking password when needed) and proves start's no-dialog
// read returns the key. Neither out nor an error sees the key.
func LoginOpencode(provider string, read, password func() ([]byte, error), k OpencodeKeychain, out io.Writer) error {
	if !launcher.ValidProvider(provider) {
		return fmt.Errorf("invalid OpenCode provider name %q", provider)
	}
	key, err := read()
	defer erase(key)
	if err != nil {
		return fmt.Errorf("reading the API key failed: %w", err)
	}
	if err := checkSecret("API key", key); err != nil {
		return err
	}
	if err := storeBrokerOnly(opencodeService, provider, "rerun "+opencodeLogin(provider), key, k, password); err != nil {
		return err
	}
	_, err = fmt.Fprintf(out, "OpenCode provider %s API key saved; keychain trusts only the broker\n", provider)
	return err
}

// OpencodeKeys is start's side of the providers' API keys.
type OpencodeKeys struct{ Keychain OpencodeKeychain }

// opencodeError maps a keychain failure for provider's key to the protocol, without its detail.
func opencodeError(provider string, err error) *protocol.Error {
	login := opencodeLogin(provider)
	switch {
	case errors.Is(err, ErrNotFound):
		return protocol.New(protocol.LoginRequired, "No API key for OpenCode provider "+provider+": run "+login+".")
	case errors.Is(err, ErrNeedsDialog):
		return protocol.New(protocol.KeychainFailed, "The OpenCode provider "+provider+" API key needs keychain approval: unlock the login keychain, or run "+login+".")
	}
	return protocol.New(protocol.KeychainFailed, "Keychain access to the OpenCode provider "+provider+" API key failed.")
}

// Ready reads only each item's access, so it never prompts.
func (o OpencodeKeys) Ready(providers []string) error {
	for _, p := range providers {
		trusted, earlier, err := checkItem(o.Keychain, opencodeService, p)
		switch {
		case err != nil:
			return opencodeError(p, err)
		case earlier:
			return protocol.New(protocol.LoginRequired, "The OpenCode provider "+p+" API key trusts an earlier broker build: run "+RetrustCommand+".")
		case !trusted:
			return protocol.New(protocol.LoginRequired, "The OpenCode provider "+p+" API key trusts another broker build: run "+opencodeLogin(p)+".")
		}
	}
	return nil
}

// Inject prepares all provider writes; on success the caller owns their buffers.
func (o OpencodeKeys) Inject(providers []string) (commands []credential.Command, err error) {
	defer func() {
		if err != nil {
			for _, command := range commands {
				command.Erase()
			}
			commands = nil
		}
	}()
	for _, p := range providers {
		key, readErr := readSecret(o.Keychain, opencodeService, p)
		if readErr != nil {
			return commands, opencodeError(p, readErr)
		}
		command, prepErr := credential.Prepare(credential.File{Dir: opencodeKeyDir, Name: p + ".key"}, key)
		erase(key)
		if prepErr != nil {
			return commands, prepErr
		}
		commands = append(commands, command)
	}
	return commands, nil
}
