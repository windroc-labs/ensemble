package broker

import (
	"bytes"
	"context"
	"encoding/hex"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

const linearSecurity = "/usr/bin/security"

// linearHostEntries is an access-only adapter. It never receives or reads root material.
type linearHostEntries struct {
	home   string
	run    func(context.Context, []string, []byte) ([]byte, error)
	access func(string, string) (AppAccess, error)
}

func runLinearSecurity(ctx context.Context, args []string, input []byte) ([]byte, error) {
	cmd := exec.CommandContext(ctx, linearSecurity, args...)
	cmd.Stdin = bytes.NewReader(input)
	return cmd.Output()
}

func (h linearHostEntries) Read(service, account string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if !linearSafe(service) || !linearSafe(account) {
		return nil, errLinear
	}
	return h.run(ctx, []string{"find-generic-password", "-s", service, "-a", account, "-w", filepath.Join(h.home, "Library/Keychains/login.keychain-db")}, nil)
}

func (h linearHostEntries) Publish(ctx context.Context, service, account string, value []byte) error {
	if !linearSafe(service) || !linearSafe(account) {
		return errLinear
	}
	kc := strconv.Quote(filepath.Join(h.home, "Library/Keychains/login.keychain-db"))
	// The account copy is durable before replacement. security -i can return success after a failed command:
	// reread both the value and trust, rather than treating its exit status as publication confirmation.
	command := "delete-generic-password -s " + service + " -a " + account + " " + kc + "\n" +
		"add-generic-password -s " + service + " -a " + account + " -T /usr/bin/security -T " + InstalledPath + " -X " + hex.EncodeToString(value) + " " + kc + "\n"
	if _, err := h.run(ctx, []string{"-i"}, []byte(command)); err != nil {
		return errLinear
	}
	stored, err := h.Read(service, account)
	defer erase(stored)
	if err != nil || !bytes.Equal(bytes.TrimSpace(stored), value) {
		return errLinear
	}
	if h.access == nil {
		return errLinear
	}
	a, err := h.access(service, account)
	if err != nil {
		return errLinear
	}
	decrypt, reader := false, false
	for _, e := range a.Entries {
		reads := false
		for _, auth := range e.Authorizations {
			if auth == "decrypt" || auth == "any" || auth == "export_clear" || auth == "export_wrapped" {
				reads = true
			}
		}
		if !reads {
			continue
		}
		if e.Any || len(e.Apps) == 0 {
			return errLinear
		}
		for _, app := range e.Apps {
			securityApp := app == linearSecurity || strings.HasPrefix(app, linearSecurity+" (")
			if app != a.Broker && !securityApp {
				return errLinear
			}
			reader = reader || securityApp
		}
		for _, auth := range e.Authorizations {
			decrypt = decrypt || auth == "decrypt" || auth == "any"
		}
	}
	if !decrypt || !reader {
		return errLinear
	}
	return nil
}
