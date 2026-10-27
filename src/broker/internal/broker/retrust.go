package broker

import (
	"errors"
	"fmt"
	"io"
	"strings"

	"ensemble/broker/internal/launcher"
)

// RetrustCommand makes every broker-owned item an earlier broker build wrote trust this one; install.sh runs it on
// every upgrade, and a stale item's error names it.
const RetrustCommand = InstalledPath + " retrust"

// codexGrantsService held each Codex container's grant record; no command writes it any more.
const codexGrantsService = "ensemble-broker-codex-grants"

// retrustServices are the services retrust covers, every KeychainServices entry in its order; envServicePrefix
// stands for each ensemble-broker-env/<name> service. Orchestrator-configured Linear services are not among them.
var retrustServices = []string{tokenService, AppKeyService, usersService, tokensService, revocationsService, codexService,
	codexGrantsService, opencodeService, envServicePrefix, linearService, linearGrantsService}

// RetrustKeychain is retrust's side of the login keychain: each item's access, its change, and the items by
// attributes only.
type RetrustKeychain interface {
	ItemTrust
	Accounts(service string) ([]string, error)
	Services(prefix string) ([]string, error)
}

type retrustItem struct{ service, account string }

// Retrust makes every item of retrustServices an earlier broker build wrote trust this build, with the login
// keychain password read once through password, and only when there is such an item. It first lists them on notice,
// then changes each with secureGuarded, guarded by earlierBuild on the access it changes. An item written by a broker
// before its stable designated requirement needs one macOS dialog, announced on notice. It never reads an item's
// value and never creates one; an item trusting another application stays unchanged, its line naming its repair.
// out gets each changed item and the count. A failed item does not stop the others: notice gets one line for each,
// and the error counts them.
func Retrust(k RetrustKeychain, password func() ([]byte, error), notice, out io.Writer) error {
	failed := 0
	fail := func(item retrustItem, err error) {
		failed++
		if item.account == "" {
			fmt.Fprintf(notice, "%q: %v; rerun %s\n", item.service, err, RetrustCommand)
			return
		}
		line := err.Error()
		if !strings.HasSuffix(line, "rerun "+RetrustCommand) && !errors.Is(err, ErrInvalidOwnerEdit) {
			line += "; rerun " + RetrustCommand
		}
		fmt.Fprintf(notice, "%q %q: %s\n", item.service, item.account, line)
	}
	foreign := 0
	leave := func(item retrustItem) {
		foreign++
		fmt.Fprintf(notice, "%q %q left unchanged: it trusts another application; %s\n", item.service, item.account, retrustRepair(item))
	}
	var listed []retrustItem
	for _, item := range retrustItems(k, fail) {
		a, err := k.AppAccess(item.service, item.account)
		switch {
		case errors.Is(err, ErrNotFound):
		case err != nil:
			fail(item, fmt.Errorf("reading its access failed: %w; rerun %s", err, RetrustCommand))
		case onlyBroker(a) == nil:
		case earlierBuild(a):
			listed = append(listed, item)
			fmt.Fprintf(notice, "will re-trust %q %q\n", item.service, item.account)
		default:
			leave(item)
		}
	}
	pw := &onePassword{read: password}
	defer pw.erase()
	if len(listed) > 0 {
		// The prompt comes first, so every guard read below follows it: an item swapped meanwhile stays unchanged.
		value, _ := pw.get()
		erase(value)
	}
	changed := 0
	for _, item := range listed {
		if a, err := k.AppAccess(item.service, item.account); err == nil && earlierBuild(a) && appsOnlyBroker(a) != nil {
			fmt.Fprintf(notice, "macOS will ask once to let ensemble-broker change access to %q %q: enter the login keychain password and click Allow\n", item.service, item.account)
		}
		err := secureGuarded(item.service, item.account, "rerun "+RetrustCommand, k, pw.get, earlierBuild)
		switch {
		case errors.Is(err, ErrNotFound): // deleted since the listing, as a missing item is skipped there
		case errors.Is(err, errNotEarlierBuild):
			leave(item)
		case err != nil:
			fail(item, err)
		default:
			changed++
			fmt.Fprintf(out, "%q %q now trusts this broker\n", item.service, item.account)
		}
	}
	if len(listed) == 0 && foreign == 0 && failed == 0 {
		_, err := fmt.Fprintln(out, "every broker keychain item already trusts this broker")
		return err
	}
	fmt.Fprintf(out, "re-trusted %d broker keychain item(s)\n", changed)
	if failed > 0 {
		return fmt.Errorf("%d broker keychain item(s) not re-trusted; rerun %s", failed, RetrustCommand)
	}
	return nil
}

// retrustItems are the items of retrustServices in their order, by attributes only: each env service's item only
// under a valid name and with that name as its account. A listing that fails is reported to fail.
func retrustItems(k RetrustKeychain, fail func(retrustItem, error)) []retrustItem {
	var items []retrustItem
	add := func(service string, only func(account string) bool) {
		accounts, err := k.Accounts(service)
		if err != nil {
			fail(retrustItem{service: service}, fmt.Errorf("listing its items failed: %w", err))
			return
		}
		for _, account := range accounts {
			if only(account) {
				items = append(items, retrustItem{service, account})
			}
		}
	}
	for _, service := range retrustServices {
		if service != envServicePrefix {
			add(service, func(string) bool { return true })
			continue
		}
		services, err := k.Services(envServicePrefix)
		if err != nil {
			fail(retrustItem{service: envServicePrefix}, fmt.Errorf("listing its items failed: %w", err))
			continue
		}
		for _, s := range services {
			if name := strings.TrimPrefix(s, envServicePrefix); launcher.ValidEnvName(name) {
				add(s, func(account string) bool { return account == name })
			}
		}
	}
	return items
}

// retrustRepair is how to repair an item retrust leaves unchanged: its login, or for broker state, deleting it.
func retrustRepair(item retrustItem) string {
	switch {
	case item.service == tokenService:
		return "run " + ClaudeLogin(item.account)
	case item.service == codexService:
		return "run " + codexLoginCommand
	case item.service == AppKeyService:
		return "run ensemble-broker login github " + AppKeyService
	case item.service == opencodeService:
		return "run " + opencodeLogin(item.account)
	case strings.HasPrefix(item.service, envServicePrefix):
		return "run " + envLogin(item.account)
	case item.service == linearService, item.service == linearGrantsService:
		return "run ensemble-broker login linear"
	}
	return "delete it in Keychain Access"
}
