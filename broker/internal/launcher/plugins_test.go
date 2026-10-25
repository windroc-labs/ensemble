package launcher

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

func TestBuildStagesPluginSourcesWithoutHostRecord(t *testing.T) {
	e := newBuildEnv(t)
	// A directory here makes any attempt to read a host install record fail.
	if err := os.MkdirAll(filepath.Join(e.home, ".claude/plugins/installed_plugins.json"), 0o700); err != nil {
		t.Fatal(err)
	}
	inner := e.fake.respond
	seen := false
	e.fake.respond = func(args []string) ([]byte, error) {
		if args[0] == "build" {
			if !slices.Contains(args, "--no-cache") {
				t.Fatal("client/plugin refresh used Docker cache")
			}
			if slices.Contains(args, "--build-arg") {
				seen = true
				data, err := os.ReadFile(filepath.Join(args[len(args)-1], "plugin-sources.json"))
				if err != nil {
					t.Fatal(err)
				}
				var sources []map[string]string
				if err := json.Unmarshal(data, &sources); err != nil || len(sources) != 2 || sources[0]["repo"] != "owner/market" || sources[1]["repo"] != "vendor/catalogue" || sources[0]["commit"] != strings.Repeat("b", 40) || sources[1]["commit"] != strings.Repeat("c", 40) || sources[1]["plugin_repo"] != "vendor/plugin" || sources[1]["plugin_commit"] != "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" {
					t.Fatalf("sources %s: %v", data, err)
				}
				if !slices.Contains(args, "ENSEMBLE_PLUGIN_DIRS=/opt/ensemble/plugins/beta:/opt/ensemble/plugins/updraft") {
					t.Fatalf("plugin dirs absent: %q", args)
				}
			}
		}
		return inner(args)
	}
	if _, err := e.host().Build(context.Background(), e.pkg, []string{"beta@owner/market#bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "updraft@vendor/plugin#aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa::vendor/catalogue#cccccccccccccccccccccccccccccccccccccccc"}, -1); err != nil {
		t.Fatal(err)
	}
	if !seen {
		t.Fatal("package build absent")
	}
}

func TestPluginSourcesChangeTheKeyButHostPluginsDoNot(t *testing.T) {
	e := newBuildEnv(t)
	build := func(names ...string) BuildResult {
		t.Helper()
		result, err := e.host().Build(context.Background(), e.pkg, names, -1)
		if err != nil {
			t.Fatal(err)
		}
		return result
	}
	none, first := build(), build("updraft@vendor/plugin#aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa::vendor/catalogue#cccccccccccccccccccccccccccccccccccccccc")
	if none.Image == first.Image {
		t.Fatal("plugins absent from key")
	}
	putFile(t, filepath.Join(e.home, ".claude/plugins/installed_plugins.json"), "invalid JSON")
	if again := build("updraft@vendor/plugin#aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa::vendor/catalogue#cccccccccccccccccccccccccccccccccccccccc"); again.Rebuilt || again.Image != first.Image {
		t.Fatal("host plugins affected image")
	}
	if next := build("updraft@owner/other#aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"); next.Image == first.Image {
		t.Fatal("repo absent from key")
	}
}

func TestPluginCommitChangesTheKey(t *testing.T) {
	e := newBuildEnv(t)
	first, err := e.host().Build(context.Background(), e.pkg, []string{"x@owner/repo#" + strings.Repeat("a", 40)}, -1)
	if err != nil {
		t.Fatal(err)
	}
	next, err := e.host().Build(context.Background(), e.pkg, []string{"x@owner/repo#" + strings.Repeat("b", 40)}, -1)
	if err != nil || first.Image == next.Image {
		t.Fatalf("commit absent from key: %v", err)
	}
}

func TestPluginSourceValidation(t *testing.T) {
	for _, names := range [][]string{{"updraft"}, {"x@owner/repo"}, {"x@owner/repo#" + strings.Repeat("a", 39)}, {"x@owner/repo#" + strings.Repeat("A", 40)}, {"x@owner/repo#" + strings.Repeat("a", 40), "y@owner/repo#" + strings.Repeat("b", 40)}, {"x@owner/repo#" + strings.Repeat("a", 40), "x@owner/other#" + strings.Repeat("a", 40)}, {"unknown"}, {"updraft@market"}, {"x@../repo"}, {"x@owner/.."}, {"x@owner/repo#ref"}, {"updraft@vendor/plugin#aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa::vendor/catalogue#cccccccccccccccccccccccccccccccccccccccc", "updraft@owner/repo"}} {
		t.Run(strings.Join(names, ","), func(t *testing.T) {
			e := newBuildEnv(t)
			_, err := e.host().Build(context.Background(), e.pkg, names, -1)
			if err == nil || !strings.Contains(err.Error(), "Plugin") || len(e.fake.calls) != 0 {
				t.Fatalf("invalid source accepted: %v; %q", err, e.fake.calls)
			}
		})
	}
}

func TestEmptyPluginSources(t *testing.T) {
	e := newBuildEnv(t)
	tmp := t.TempDir()
	_, names, err := e.host().stage(e.pkg, nil, tmp)
	if err != nil || len(names) != 0 {
		t.Fatalf("%q %v", names, err)
	}
	data, err := os.ReadFile(filepath.Join(tmp, "package/plugin-sources.json"))
	if err != nil || string(data) != "[]" {
		t.Fatalf("%s %v", data, err)
	}
}

func TestPluginPinsCannotConflictBehindSharedMarketplace(t *testing.T) {
	e := newBuildEnv(t)
	_, err := e.host().Build(context.Background(), e.pkg, []string{
		"updraft@vendor/plugin#" + strings.Repeat("a", 40) + "::vendor/catalogue#" + strings.Repeat("c", 40),
		"beta@vendor/plugin#" + strings.Repeat("b", 40) + "::vendor/catalogue#" + strings.Repeat("c", 40),
	}, -1)
	if err == nil || !strings.Contains(err.Error(), "conflicting commits") || len(e.fake.calls) != 0 {
		t.Fatalf("conflicting upstream pins accepted: %v; %q", err, e.fake.calls)
	}
}

func TestConfiguredMarketplacePinsAffectImageKey(t *testing.T) {
	e := newBuildEnv(t)
	build := func(pluginRepo, pluginCommit, marketRepo, marketCommit string) BuildResult {
		t.Helper()
		value := "widget@" + pluginRepo + "#" + pluginCommit + "::" + marketRepo + "#" + marketCommit
		result, err := e.host().Build(context.Background(), e.pkg, []string{value}, -1)
		if err != nil {
			t.Fatal(err)
		}
		return result
	}
	a, b := strings.Repeat("a", 40), strings.Repeat("b", 40)
	first := build("custom/plugin", a, "other/catalogue", a)
	for _, next := range []BuildResult{
		build("custom/plugin", b, "other/catalogue", a),
		build("custom/renamed", a, "other/catalogue", a),
		build("custom/plugin", a, "other/catalogue", b),
		build("custom/plugin", a, "other/renamed", a),
	} {
		if first.Image == next.Image {
			t.Fatal("configured plugin or marketplace input absent from image key")
		}
	}
}

func TestMarketplacePinsCannotConflictBetweenPlugins(t *testing.T) {
	e := newBuildEnv(t)
	_, err := e.host().Build(context.Background(), e.pkg, []string{
		"one@first/plugin#" + strings.Repeat("a", 40) + "::shared/catalogue#" + strings.Repeat("a", 40),
		"two@second/plugin#" + strings.Repeat("b", 40) + "::shared/catalogue#" + strings.Repeat("b", 40),
	}, -1)
	if err == nil || !strings.Contains(err.Error(), "conflicting commits for marketplace") || len(e.fake.calls) != 0 {
		t.Fatalf("conflicting catalogue pins accepted: %v; %q", err, e.fake.calls)
	}
}

func TestMalformedConfiguredMarketplacesFailBeforeDocker(t *testing.T) {
	for _, suffix := range []string{"::", "::owner/catalogue", "::owner/catalogue#main", "::../catalogue#" + strings.Repeat("a", 40), "::owner/catalogue#" + strings.Repeat("A", 40)} {
		e := newBuildEnv(t)
		_, err := e.host().Build(context.Background(), e.pkg, []string{"widget@custom/plugin#" + strings.Repeat("a", 40) + suffix}, -1)
		if err == nil || len(e.fake.calls) != 0 {
			t.Fatalf("invalid marketplace suffix accepted: %q: %v", suffix, err)
		}
	}
}
