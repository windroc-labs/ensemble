package broker

import (
	"bytes"
	"errors"
	"fmt"
	"io"

	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/launcher"
	"ensemble/broker/internal/protocol"
)

// An OpenCode provider's API key: service opencodeService, account the provider's name; in a container, the file
// opencodeKeyDir/<provider>.key.
const (
	opencodeService = "ensemble-opencode"
	opencodeKeyDir  = docker.SecretsDir + "/opencode"
)

// opencodeScript writes stdin to $1's key file, 0400; set -C refuses one already there.
const opencodeScript = "mkdir -p -m 0700 " + opencodeKeyDir + " && umask 0377 && set -C && cat > " + opencodeKeyDir + `/"$1".key`

// OpencodeKeychain holds the providers' API keys as broker-only items.
type OpencodeKeychain interface {
	SecretKeychain
	ItemTrust
}

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
	item, rerun := opencodeService+" "+provider, "rerun ensemble-broker login opencode "+provider
	if err := k.PutSecret(opencodeService, provider, key); err != nil {
		return fmt.Errorf("keychain item %s: saving it failed: %w; %s", item, err, rerun)
	}
	if err := secureItem(opencodeService, provider, rerun+" to finish", k, password); err != nil {
		return err
	}
	saved, err := k.Secret(opencodeService, provider)
	defer erase(saved)
	if err == nil && !bytes.Equal(saved, key) {
		err = errors.New("it holds other bytes")
	}
	if err != nil {
		return fmt.Errorf("keychain item %s: reading it failed: %w; %s", item, err, rerun)
	}
	_, err = fmt.Fprintf(out, "OpenCode provider %s API key saved; keychain trusts only the broker\n", provider)
	return err
}

// OpencodeKeys is start's side of the providers' API keys.
type OpencodeKeys struct{ Keychain OpencodeKeychain }

// opencodeError maps a keychain failure for provider's key to the protocol, without its detail.
func opencodeError(provider string, err error) *protocol.Error {
	login := "ensemble-broker login opencode " + provider
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
		access, err := o.Keychain.AppAccess(opencodeService, p)
		if err != nil {
			return opencodeError(p, err)
		}
		if onlyBroker(access) != nil {
			return protocol.New(protocol.LoginRequired, "The OpenCode provider "+p+" API key trusts another broker build: run ensemble-broker login opencode "+p+".")
		}
	}
	return nil
}

// Inject writes each provider's key into the container's secrets tmpfs through exec's stdin, never showing a keychain
// dialog. Errors are opencodeError's, wrap docker.ErrUnreachable or are opaque: no Docker stderr, no keychain detail.
func (o OpencodeKeys) Inject(providers []string, exec func(argv []string, stdin []byte) error) error {
	for _, p := range providers {
		key, err := o.Keychain.Secret(opencodeService, p)
		if err != nil {
			erase(key)
			return opencodeError(p, err)
		}
		err = exec([]string{"/bin/sh", "-c", opencodeScript, "sh", p}, key)
		erase(key)
		if errors.Is(err, docker.ErrUnreachable) {
			return fmt.Errorf("API key injection: %w", docker.ErrUnreachable)
		}
		if err != nil {
			return errors.New("writing an OpenCode provider API key into the container failed")
		}
	}
	return nil
}
