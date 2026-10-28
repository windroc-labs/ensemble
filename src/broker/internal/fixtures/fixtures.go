// Package fixtures reads the shared protocol fixtures for tests.
package fixtures

import (
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

type Context struct {
	Home                string `json:"home"`
	CWDExists           bool   `json:"cwd_exists"`
	Volume              string `json:"volume"`
	ClaudeDirHasSymlink bool   `json:"claude_dir_has_symlink"`
	Login               string `json:"login"`
	Image               string `json:"image"`
	Docker              string `json:"docker"`
	Egress              string `json:"egress"` // the egress proxy's state; "": healthy
}

// Case is one entry of a definition's table, <definition>.json.
type Case struct {
	Name       string `json:"name"`
	Definition string `json:"-"`
	Valid      bool   `json:"valid"`
	Expected   struct {
		Stage     string  `json:"stage"`
		ExitCode  *int    `json:"exit_code"`
		ErrorCode *string `json:"error_code"`
	} `json:"expected"`
	Context *Context        `json:"context"`
	Data    json.RawMessage `json:"data"`
}

func Dir() string {
	_, file, _, _ := runtime.Caller(0)
	return filepath.Join(filepath.Dir(file), "../../protocol/docker-broker/fixtures")
}

// Cases returns the cases of the tables of defs, in order (every table when empty).
func Cases(t *testing.T, defs ...string) []Case {
	t.Helper()
	if len(defs) == 0 {
		paths, err := filepath.Glob(filepath.Join(Dir(), "*.json"))
		if err != nil {
			t.Fatal(err)
		}
		for _, p := range paths {
			defs = append(defs, strings.TrimSuffix(filepath.Base(p), ".json"))
		}
	}
	var out []Case
	for _, d := range defs {
		data, err := os.ReadFile(filepath.Join(Dir(), d+".json"))
		if err != nil {
			t.Fatal(err)
		}
		var table []Case
		if err := json.Unmarshal(data, &table); err != nil {
			t.Fatalf("%s: %v", d, err)
		}
		for _, c := range table {
			c.Definition = d
			out = append(out, c)
		}
	}
	if len(out) == 0 {
		t.Fatalf("no fixtures for %v", defs)
	}
	return out
}

// Named returns the data of the case called name, from any table.
func Named(t *testing.T, name string) []byte {
	t.Helper()
	for _, c := range Cases(t) {
		if c.Name == name {
			return c.Data
		}
	}
	t.Fatalf("no fixture %s", name)
	return nil
}

// SameJSON reports whether a and b hold equal JSON values.
func SameJSON(t *testing.T, a, b []byte) bool {
	t.Helper()
	var x, y any
	if err := json.Unmarshal(a, &x); err != nil {
		t.Fatalf("%v: %s", err, a)
	}
	if err := json.Unmarshal(b, &y); err != nil {
		t.Fatalf("%v: %s", err, b)
	}
	ja, _ := json.Marshal(x)
	jb, _ := json.Marshal(y)
	return string(ja) == string(jb)
}
