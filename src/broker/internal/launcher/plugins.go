package launcher

import (
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"

	"ensemble/broker/internal/protocol"
)

const (
	pluginName = `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`
	pluginRepo = `[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}`
)

// PluginNamePattern is a plugin's name; PluginSourcePattern a repository source naming one,
// NAME@OWNER/REPO#COMMIT[::MARKETPLACE-OWNER/REPO#COMMIT].
var (
	PluginNamePattern   = regexp.MustCompile(`^` + pluginName + `$`)
	PluginSourcePattern = regexp.MustCompile(`^(` + pluginName + `)@(` + pluginRepo + `)#([a-f0-9]{40})(?:::(` + pluginRepo + `)#([a-f0-9]{40}))?$`)
)

const pluginRoot = "/opt/ensemble/plugins/"

type pluginSource struct {
	Name         string `json:"name"`
	Repo         string `json:"repo"`
	Commit       string `json:"commit"`
	PluginRepo   string `json:"plugin_repo,omitempty"`
	PluginCommit string `json:"plugin_commit,omitempty"`
}

// stagePlugins records repository sources, never host client installation state.
func stagePlugins(names []string, dir string) ([]string, error) {
	sources := []pluginSource{}
	for _, value := range names {
		match := PluginSourcePattern.FindStringSubmatch(value)
		if match == nil {
			return nil, protocol.New(protocol.BuildFailed, "Plugin "+value+": use name@owner/repo#40-hex-commit, optionally followed by ::marketplace-owner/repo#40-hex-commit.")
		}
		name, repo, commit := match[1], match[2], match[3]
		source := pluginSource{Name: name, Repo: repo, Commit: commit}
		if match[4] != "" {
			source.Repo, source.Commit = match[4], match[5]
			source.PluginRepo, source.PluginCommit = repo, commit
		}
		for _, previous := range sources {
			if previous.Name == name {
				return nil, protocol.New(protocol.BuildFailed, "Plugin "+name+": duplicate plugin name.")
			}
			previousRepo, previousCommit := previous.Repo, previous.Commit
			if previous.PluginRepo != "" {
				previousRepo, previousCommit = previous.PluginRepo, previous.PluginCommit
			}
			if previousRepo == repo && previousCommit != commit {
				return nil, protocol.New(protocol.BuildFailed, "Plugin "+name+": conflicting commits for "+repo+".")
			}
			if previous.Repo == source.Repo && previous.Commit != source.Commit {
				return nil, protocol.New(protocol.BuildFailed, "Plugin "+name+": conflicting commits for marketplace "+source.Repo+".")
			}
		}
		sources = append(sources, source)
	}
	slices.SortFunc(sources, func(a, b pluginSource) int { return strings.Compare(a.Name, b.Name) })
	data, err := json.Marshal(sources)
	if err == nil {
		err = os.WriteFile(filepath.Join(dir, "plugin-sources.json"), data, 0o644)
	}
	if err != nil {
		return nil, protocol.New(protocol.BuildFailed, "Cannot stage plugin sources.")
	}
	staged := make([]string, len(sources))
	for i, source := range sources {
		staged[i] = source.Name
	}
	return staged, nil
}

func pluginDirs(names []string) string {
	dirs := make([]string, len(names))
	for i, name := range names {
		dirs[i] = pluginRoot + name
	}
	return strings.Join(dirs, ":")
}
