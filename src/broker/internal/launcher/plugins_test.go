package launcher

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"ensemble/broker/internal/protocol"
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
	if _, err := e.host().Build(context.Background(), e.pkg, BuildSpec{Plugins: []string{"beta@owner/market#bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "updraft@vendor/plugin#aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa::vendor/catalogue#cccccccccccccccccccccccccccccccccccccccc"}}, -1); err != nil {
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
		result, err := e.host().Build(context.Background(), e.pkg, BuildSpec{Plugins: names}, -1)
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
	first, err := e.host().Build(context.Background(), e.pkg, BuildSpec{Plugins: []string{"x@owner/repo#" + strings.Repeat("a", 40)}}, -1)
	if err != nil {
		t.Fatal(err)
	}
	next, err := e.host().Build(context.Background(), e.pkg, BuildSpec{Plugins: []string{"x@owner/repo#" + strings.Repeat("b", 40)}}, -1)
	if err != nil || first.Image == next.Image {
		t.Fatalf("commit absent from key: %v", err)
	}
}

func TestPluginSourceValidation(t *testing.T) {
	a, b, c := strings.Repeat("a", 40), strings.Repeat("b", 40), strings.Repeat("c", 40)
	widget := "widget@custom/plugin#" + a
	const format = ": use name@owner/repo#40-hex-commit"
	for _, tc := range []struct {
		want  string
		names []string
	}{
		{format, []string{"updraft"}}, {format, []string{"x@owner/repo"}},
		{format, []string{"x@owner/repo#" + strings.Repeat("a", 39)}}, {format, []string{"x@owner/repo#" + strings.Repeat("A", 40)}},
		{"Plugin y: conflicting commits for owner/repo.", []string{"x@owner/repo#" + a, "y@owner/repo#" + b}},
		{"Plugin x: duplicate plugin name.", []string{"x@owner/repo#" + a, "x@owner/other#" + a}},
		{format, []string{"unknown"}}, {format, []string{"updraft@market"}}, {format, []string{"x@../repo"}},
		{format, []string{"x@owner/.."}}, {format, []string{"x@owner/repo#ref"}},
		{format, []string{"updraft@vendor/plugin#" + a + "::vendor/catalogue#" + c, "updraft@owner/repo"}},
		// One commit per plugin repository, also behind a shared marketplace, and per marketplace.
		{"Plugin beta: conflicting commits for vendor/plugin.",
			[]string{"updraft@vendor/plugin#" + a + "::vendor/catalogue#" + c, "beta@vendor/plugin#" + b + "::vendor/catalogue#" + c}},
		{"Plugin two: conflicting commits for marketplace shared/catalogue.",
			[]string{"one@first/plugin#" + a + "::shared/catalogue#" + a, "two@second/plugin#" + b + "::shared/catalogue#" + b}},
		// Malformed configured marketplaces.
		{format, []string{widget + "::"}}, {format, []string{widget + "::owner/catalogue"}},
		{format, []string{widget + "::owner/catalogue#main"}}, {format, []string{widget + "::../catalogue#" + a}},
		{format, []string{widget + "::owner/catalogue#" + strings.Repeat("A", 40)}},
	} {
		t.Run(strings.Join(tc.names, ","), func(t *testing.T) {
			e := newBuildEnv(t)
			_, err := e.host().Build(context.Background(), e.pkg, BuildSpec{Plugins: tc.names}, -1)
			if err == nil || !strings.Contains(err.Error(), "Plugin") || !strings.Contains(err.Error(), tc.want) ||
				len(e.fake.calls) != 0 {
				t.Fatalf("invalid source accepted: %v; %q", err, e.fake.calls)
			}
		})
	}
}

func TestEmptyPluginSources(t *testing.T) {
	e := newBuildEnv(t)
	tmp := t.TempDir()
	_, dirs, err := e.host().stage(e.pkg, BuildSpec{}, tmp)
	if err != nil || dirs != "" {
		t.Fatalf("%q %v", dirs, err)
	}
	data, err := os.ReadFile(filepath.Join(tmp, "package/plugin-sources.json"))
	if err != nil || string(data) != "[]" {
		t.Fatalf("%s %v", data, err)
	}
}

func TestConfiguredMarketplacePinsAffectImageKey(t *testing.T) {
	e := newBuildEnv(t)
	build := func(pluginRepo, pluginCommit, marketRepo, marketCommit string) BuildResult {
		t.Helper()
		value := "widget@" + pluginRepo + "#" + pluginCommit + "::" + marketRepo + "#" + marketCommit
		result, err := e.host().Build(context.Background(), e.pkg, BuildSpec{Plugins: []string{value}}, -1)
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

func TestPluginNameGrammar(t *testing.T) {
	long := strings.Repeat("a", 64)
	for name, want := range map[string]bool{"a": true, "a.b-c_d": true, long: true, long + "a": false, "../x": false, ".x": false, "-x": false, "a/b": false, "": false, "a@b": false} {
		if got := PluginNamePattern.MatchString(name); got != want {
			t.Errorf("PluginNamePattern(%q) = %v", name, got)
		}
		if got := PluginSourcePattern.MatchString(name + "@owner/repo#" + strings.Repeat("a", 40)); got != want {
			t.Errorf("PluginSourcePattern name %q = %v", name, got)
		}
	}
}

// packageBuild is the ensemble-layer build among calls, nil when absent.
func packageBuild(calls [][]string) []string {
	for _, c := range calls {
		if c[0] == "build" && slices.Contains(c, "--build-arg") {
			return c
		}
	}
	return nil
}

func TestImagePluginDuplicates(t *testing.T) {
	source := "a@owner/repo#" + strings.Repeat("a", 40)
	for name, spec := range map[string]BuildSpec{
		"twice":       {ImagePlugins: []string{"a", "b", "a"}},
		"also source": {Plugins: []string{source}, ImagePlugins: []string{"a"}},
		"base image":  {BaseImage: baseRef, ImagePlugins: []string{"a", "a"}},
	} {
		t.Run(name, func(t *testing.T) {
			e := newBuildEnv(t)
			_, err := e.host().Build(context.Background(), e.pkg, spec, -1)
			var pe *protocol.Error
			if !errors.As(err, &pe) || pe.Code != protocol.BuildFailed || pe.Message != "Plugin a: duplicate plugin name." || len(e.fake.calls) != 0 {
				t.Fatalf("%v %q", err, e.fake.verbs())
			}
		})
	}
	e := newBuildEnv(t)
	_, err := e.host().Build(context.Background(), e.pkg, BuildSpec{ImagePlugins: []string{"../x"}}, -1)
	var pe *protocol.Error
	if !errors.As(err, &pe) || pe.Code != protocol.BuildFailed || !strings.Contains(pe.Message, "../x") || len(e.fake.calls) != 0 {
		t.Fatalf("%v %q", err, e.fake.verbs())
	}
}

func TestImagePluginsAreInTheKey(t *testing.T) {
	e := newBuildEnv(t)
	key := func(names ...string) string {
		t.Helper()
		result, err := e.host().Build(context.Background(), e.pkg, BuildSpec{ImagePlugins: names}, -1)
		if err != nil {
			t.Fatal(err)
		}
		return result.Image
	}
	none, a, forward, reversed, c := key(), key("a"), key("a", "b"), key("b", "a"), key("c")
	if forward != reversed {
		t.Fatal("image plugin order changed the key")
	}
	if len(map[string]bool{none: true, a: true, forward: true, c: true}) != 4 {
		t.Fatalf("keys collide: %s %s %s %s", none, a, forward, c)
	}
}

func TestDefaultBuildContextUnchanged(t *testing.T) {
	e := newBuildEnv(t)
	inner := e.fake.respond
	var root []string
	e.fake.respond = func(args []string) ([]byte, error) {
		if args[0] == "build" && slices.Contains(args, "--build-arg") {
			entries, err := os.ReadDir(filepath.Dir(args[len(args)-1]))
			if err != nil {
				t.Fatal(err)
			}
			for _, entry := range entries {
				root = append(root, entry.Name())
			}
			if _, err := os.Stat(filepath.Join(args[len(args)-1], "image-plugins")); !os.IsNotExist(err) {
				t.Fatal("image plugins staged in the package context")
			}
		}
		return inner(args)
	}
	if _, err := e.host().Build(context.Background(), e.pkg, BuildSpec{}, -1); err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(root, []string{"package", "user"}) {
		t.Fatalf("build root %q", root)
	}
	if slices.ContainsFunc(e.fake.calls, func(c []string) bool { return slices.Contains(c, "node") }) {
		t.Fatal("plugin check without in-image plugins")
	}
}
