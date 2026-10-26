package launcher

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"ensemble/broker/internal/codex"
)

// deadlineReleases is fakeReleases that also records each lookup's deadline.
type deadlineReleases struct {
	fakeReleases
	deadlines []time.Time
}

func (d *deadlineReleases) Latest(ctx context.Context, pkg string) (string, error) {
	deadline, _ := ctx.Deadline()
	d.deadlines = append(d.deadlines, deadline)
	return d.fakeReleases.Latest(ctx, pkg)
}

func TestLatestClients(t *testing.T) {
	r := &deadlineReleases{fakeReleases: *newReleases()}
	got, err := Host{Releases: r}.latestClients(context.Background())
	end := time.Now()
	if err != nil || got != (ClientVersions{Claude: "2.1.7", Codex: "0.170.0", Opencode: "1.20.1"}) {
		t.Fatalf("%+v %v", got, err)
	}
	for _, deadline := range r.deadlines {
		if deadline.IsZero() || deadline.After(end.Add(2*time.Minute)) {
			t.Fatalf("lookup deadline %v", deadline)
		}
	}
	at := func(pkg, version string) map[string]string {
		latest := newReleases().latest
		latest[pkg] = version
		return latest
	}
	for name, tc := range map[string]struct {
		latest map[string]string
		err    error
		want   string
	}{
		"claude below its minimum":   {latest: at("@anthropic-ai/claude-code", "2.0.99"), want: "@anthropic-ai/claude-code"},
		"codex below its minimum":    {latest: at("@openai/codex", "0.162.9"), want: "@openai/codex"},
		"opencode below its minimum": {latest: at("opencode-ai", "1.19.2"), want: "opencode-ai"},
		"a prerelease":               {latest: at("opencode-ai", "1.20.1-beta.1"), want: "opencode-ai"},
		"not a version":              {latest: at("@openai/codex", "latest"), want: "@openai/codex"},
		"a lookup failure":           {err: errors.New("@anthropic-ai/claude-code registry unavailable"), want: "@anthropic-ai/claude-code registry unavailable"},
	} {
		t.Run(name, func(t *testing.T) {
			_, err := Host{Releases: &fakeReleases{latest: tc.latest, err: tc.err}}.latestClients(context.Background())
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("%v", err)
			}
		})
	}
	minimums := map[string]string{"@anthropic-ai/claude-code": "2.1.0", "@openai/codex": codex.MinVersion, "opencode-ai": opencodeMinVersion}
	if _, err := (Host{Releases: &fakeReleases{latest: minimums}}).latestClients(context.Background()); err != nil {
		t.Fatalf("the minimums refused: %v", err)
	}
	if _, err := (Host{}).latestClients(context.Background()); err == nil {
		t.Fatal("no Releases looked up nothing and passed")
	}
}

func TestImageClients(t *testing.T) {
	labels := map[string]string{"ensemble.claude-version": "2.1.7", "ensemble.codex-version": "0.170.0", "ensemble.opencode-version": "1.20.1", "ensemble.key": "k"}
	got, ok := imageClients(labels)
	if want := (ClientVersions{Claude: "2.1.7", Codex: "0.170.0", Opencode: "1.20.1"}); !ok || got != want {
		t.Fatalf("%+v %v", got, ok)
	}
	for label := range clientArgs {
		for _, bad := range []string{"", "1.2", "v1.2.3", "1.2.3-rc.1", "01.2.3"} {
			changed := map[string]string{}
			for k, v := range labels {
				changed[k] = v
			}
			changed[clientArgs[label]] = bad
			if _, ok := imageClients(changed); ok {
				t.Fatalf("%s=%q accepted", clientArgs[label], bad)
			}
		}
	}
	if _, ok := imageClients(nil); ok {
		t.Fatal("no labels accepted")
	}
}

func TestClientVersionsDiffer(t *testing.T) {
	v := ClientVersions{Claude: "2.1.7", Codex: "0.170.0", Opencode: "1.20.1"}
	if v.differs(v) {
		t.Fatal("equal versions differ")
	}
	for _, other := range []ClientVersions{
		{Claude: "2.1.8", Codex: "0.170.0", Opencode: "1.20.1"},
		{Claude: "2.1.7", Codex: "0.169.0", Opencode: "1.20.1"},
		{Claude: "2.1.7", Codex: "0.170.0", Opencode: "1.20.2"},
	} {
		if !v.differs(other) {
			t.Fatalf("%+v does not differ from %+v", other, v)
		}
	}
}
