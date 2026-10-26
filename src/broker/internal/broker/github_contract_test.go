package broker

import (
	"context"
	"errors"
	"net/http"
	"slices"
	"strings"
	"testing"

	"ensemble/broker/internal/credential"
	"ensemble/broker/internal/protocol"
)

// rebuilt is a fresh GitHub over r's persistent fake Keychain and fake GitHub: what the next broker command sees.
func (r *rig) rebuilt() GitHub {
	return GitHub{Keychain: r.keys, Apps: r.g.Apps, API: r.g.API, Lock: r.g.Lock}
}

func (r *rig) states(t *testing.T) map[string]credential.State {
	t.Helper()
	out := map[string]credential.State{}
	if rec, ok := r.record(t); ok {
		for _, e := range rec.Tokens {
			out[e.Token] = e.Current()
		}
	}
	return out
}

func (r *rig) queued(t *testing.T) []string {
	t.Helper()
	var out []string
	for _, p := range r.backlog(t) {
		out = append(out, p.Token)
	}
	return out
}

// written is an exec into the container that fails its nth write (0: none) and keeps each account's last token.
type written struct {
	failAt, n int
	files     map[string]string
}

func (w *written) exec(argv []string, stdin []byte) error {
	w.n++
	if w.n == w.failAt {
		return errors.New("exec failed")
	}
	if w.files == nil {
		w.files = map[string]string{}
	}
	w.files[argv[len(argv)-1]] = string(stdin)
	return nil
}

func TestGitHubContractReconstruction(t *testing.T) {
	ctx := context.Background()
	delivered := credential.Delivered
	t.Run("issue", func(t *testing.T) {
		r := newRig(t, "acme", "Other-Org")
		w := &written{}
		if _, err := r.g.Issue(ctx, "ensemble-bot", perms, ctr, w.exec); err != nil {
			t.Fatal(err)
		}
		if got := r.states(t); len(got) != 2 || got["ghs_1_1"] != delivered || got["ghs_2_2"] != delivered {
			t.Fatalf("record %v", got)
		}
		if w.files["acme"] != "ghs_1_1" || w.files["Other-Org"] != "ghs_2_2" {
			t.Fatalf("files %v", w.files)
		}
		if err := r.rebuilt().Release(ctx, ctr); err != nil || !slices.Equal(r.api.revoked, []string{"ghs_1_1", "ghs_2_2"}) {
			t.Fatalf("rebuilt release: %v, revoked %q", err, r.api.revoked)
		}
	})
	t.Run("refresh", func(t *testing.T) {
		r := newRig(t, "acme")
		if _, err := r.g.Issue(ctx, "ensemble-bot", perms, ctr, (&written{}).exec); err != nil {
			t.Fatal(err)
		}
		w := &written{}
		got, err := r.rebuilt().Refresh(ctx, "ensemble-bot", nil, ctr, w.exec)
		if err != nil || len(got) != 1 || w.files["acme"] != "ghs_1_2" {
			t.Fatalf("%v %v %v", got, err, w.files)
		}
		if states := r.states(t); len(states) != 1 || states["ghs_1_2"] != delivered || !slices.Equal(r.api.revoked, []string{"ghs_1_1"}) {
			t.Fatalf("record %v revoked %q", states, r.api.revoked)
		}
		if r.api.bodies[1] != permsBody {
			t.Fatalf("replacement not limited to the recorded permissions: %s", r.api.bodies[1])
		}
	})
	t.Run("release", func(t *testing.T) {
		r := newRig(t, "acme")
		if _, err := r.g.Issue(ctx, "ensemble-bot", perms, ctr, (&written{}).exec); err != nil {
			t.Fatal(err)
		}
		r.api.revokeErr = func(string) int { return http.StatusServiceUnavailable }
		if err := r.g.Release(ctx, ctr); err != nil {
			t.Fatal(err)
		}
		if _, ok := r.record(t); ok || !slices.Equal(r.queued(t), []string{"ghs_1_1"}) {
			t.Fatalf("release did not transfer to the backlog: %v", r.queued(t))
		}
		r.api.revokeErr = nil
		if left, err := r.rebuilt().Retry(ctx); err != nil || len(left) != 0 || !slices.Equal(r.api.revoked, []string{"ghs_1_1"}) {
			t.Fatalf("rebuilt retry: %v %v %q", left, err, r.api.revoked)
		}
	})
	t.Run("start rollback", func(t *testing.T) {
		r := newRig(t, "acme", "Other-Org")
		r.api.revokeErr = func(string) int { return http.StatusServiceUnavailable }
		r.keys.unreadable[revocationsService] = true
		if _, err := r.g.Issue(ctx, "ensemble-bot", perms, ctr, (&written{failAt: 2}).exec); err == nil {
			t.Fatal("issue succeeded")
		}
		if got := r.states(t); len(got) != 2 || got["ghs_1_1"] == delivered || got["ghs_2_2"] == delivered {
			t.Fatalf("rolled-back tokens not kept undelivered: %v", got)
		}
		r.api.revokeErr, r.keys.unreadable = nil, map[string]bool{}
		if err := r.rebuilt().Release(ctx, ctr); err != nil {
			t.Fatal(err)
		}
		if _, ok := r.record(t); ok || len(r.queued(t)) != 0 || !slices.Equal(r.api.revoked, []string{"ghs_1_1", "ghs_2_2"}) {
			t.Fatalf("rebuilt release left %v %q", r.queued(t), r.api.revoked)
		}
	})
	t.Run("refresh rollback", func(t *testing.T) {
		r := newRig(t, "acme")
		if _, err := r.g.Issue(ctx, "ensemble-bot", perms, ctr, (&written{}).exec); err != nil {
			t.Fatal(err)
		}
		r.api.revokeErr = func(string) int { return http.StatusServiceUnavailable }
		r.keys.unreadable[revocationsService] = true
		if _, err := r.g.Refresh(ctx, "ensemble-bot", perms, ctr, (&written{failAt: 1}).exec); err == nil {
			t.Fatal("refresh succeeded")
		}
		if got := r.states(t); got["ghs_1_1"] != delivered || got["ghs_1_2"] == delivered {
			t.Fatalf("old token not kept usable: %v", got)
		}
		r.api.revokeErr, r.keys.unreadable = nil, map[string]bool{}
		w := &written{}
		if _, err := r.rebuilt().Refresh(ctx, "ensemble-bot", perms, ctr, w.exec); err != nil {
			t.Fatal(err)
		}
		if got := r.states(t); len(got) != 1 || got["ghs_1_3"] != delivered || w.files["acme"] != "ghs_1_3" {
			t.Fatalf("rebuilt refresh left %v", got)
		}
		for _, token := range []string{"ghs_1_1", "ghs_1_2"} {
			if !slices.Contains(r.api.revoked, token) {
				t.Fatalf("%s not retired: %q", token, r.api.revoked)
			}
		}
	})
}

func TestRefreshInjectionFailureKeepsOldToken(t *testing.T) {
	r := newRig(t, "acme")
	if _, err := r.g.Issue(context.Background(), "ensemble-bot", perms, ctr, (&written{}).exec); err != nil {
		t.Fatal(err)
	}
	_, err := r.g.Refresh(context.Background(), "ensemble-bot", perms, ctr, (&written{failAt: 1}).exec)
	if err == nil || strings.Contains(err.Error(), "ghs_") {
		t.Fatalf("%v", err)
	}
	if got := r.states(t); len(got) != 1 || got["ghs_1_1"] != credential.Delivered || !slices.Equal(r.api.revoked, []string{"ghs_1_2"}) {
		t.Fatalf("record %v revoked %q", got, r.api.revoked)
	}
}

func TestGitHubFileRefusesBadAccount(t *testing.T) {
	p := githubProvider{}
	for _, account := range []string{"../claude", ".hidden", "a/b", ""} {
		if _, err := p.File(credential.Material{Account: account}); err == nil || err.Error() != `account "`+account+`" is not a GitHub login` {
			t.Errorf("%q: %v", account, err)
		}
	}
	if f, err := p.File(credential.Material{Account: "Other-Org"}); err != nil || f != (credential.File{Dir: tokenDir, Name: "Other-Org", Replace: true}) {
		t.Fatalf("%+v %v", f, err)
	}
}

// sentinelKeychain fails every read of service with a sentinel in its error text.
type sentinelKeychain struct {
	*secrets
	service string
}

func (k sentinelKeychain) Secret(service, account string) ([]byte, error) {
	if service == k.service {
		return nil, errors.New("keychain status -25293: KEYCHAIN_SENTINEL")
	}
	return k.secrets.Secret(service, account)
}

func TestGitHubSentinelRedacted(t *testing.T) {
	ctx := context.Background()
	r := newRig(t, "acme")
	rec, sink := r.audited("close", ctr)
	if _, err := r.g.Issue(ctx, "ensemble-bot", perms, ctr, (&written{}).exec); err != nil {
		t.Fatal(err)
	}
	r.api.revokeErr = func(string) int { return http.StatusInternalServerError }
	if err := r.g.Release(ctx, ctr); err != nil {
		t.Fatal(err)
	}
	for _, service := range []string{tokensService, revocationsService} {
		g := r.rebuilt()
		g.Keychain = sentinelKeychain{r.keys, service}
		var errs []error
		_, err := g.Pending()
		errs = append(errs, err, g.Release(ctx, ctr))
		_, err = g.Refresh(ctx, "ensemble-bot", perms, ctr, (&written{}).exec)
		errs = append(errs, err)
		_, err = g.Retry(ctx)
		errs = append(errs, err)
		for _, err := range errs {
			if err != nil && strings.Contains(err.Error(), "KEYCHAIN_SENTINEL") {
				t.Fatalf("%s: %v", service, err)
			}
		}
		var e *protocol.Error
		if !errors.As(errs[2], &e) || e.Code != protocol.KeychainFailed && e.Code != protocol.RefreshFailed {
			t.Fatalf("%s refresh: %v", service, errs[2])
		}
	}
	for _, l := range flush(t, rec, sink) {
		for _, c := range l.Credentials {
			if strings.Contains(c.Reason+c.ReposError, "SENTINEL") || strings.Contains(c.Reason, "ghs_") {
				t.Fatalf("audit leaks: %+v", c)
			}
		}
	}
}
