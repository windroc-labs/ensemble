// Package fixtures reads the shared protocol fixtures for tests.
package fixtures

import (
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
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

type Case struct {
	File       string `json:"file"`
	Definition string `json:"definition"`
	Valid      bool   `json:"valid"`
	Expected   struct {
		Stage     string  `json:"stage"`
		ExitCode  *int    `json:"exit_code"`
		ErrorCode *string `json:"error_code"`
	} `json:"expected"`
	Context *Context `json:"context"`
	Data    []byte   `json:"-"`
}

func Dir() string {
	_, file, _, _ := runtime.Caller(0)
	return filepath.Join(filepath.Dir(file), "../../protocol/docker-broker/fixtures")
}

// Cases returns every indexed fixture whose definition is one of defs (all when empty).
func Cases(t *testing.T, defs ...string) []Case {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(Dir(), "index.json"))
	if err != nil {
		t.Fatal(err)
	}
	var index struct {
		Cases []Case `json:"cases"`
	}
	if err := json.Unmarshal(data, &index); err != nil {
		t.Fatal(err)
	}
	want := map[string]bool{}
	for _, d := range defs {
		want[d] = true
	}
	var out []Case
	for _, c := range index.Cases {
		if len(defs) > 0 && !want[c.Definition] {
			continue
		}
		if c.Data, err = os.ReadFile(filepath.Join(Dir(), c.File)); err != nil {
			t.Fatal(err)
		}
		out = append(out, c)
	}
	if len(out) == 0 {
		t.Fatalf("no fixtures for %v", defs)
	}
	return out
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
