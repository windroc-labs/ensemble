package codex

import (
	"archive/tar"
	"bytes"
	"context"
	"crypto/sha512"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"strings"
	"testing"
)

func TestVersionOrdering(t *testing.T) {
	for _, c := range []struct {
		a, b string
		want bool
	}{
		{"0.162.10", "0.162.9", true}, {"1.0.0", "0.999.0", true},
		{"0.163.0", "0.162.999", true}, {"0.162.1", "0.162.1", false},
		{"0.162.1-beta", "0.162.0", false}, {"01.1.1", "0.0.0", false},
		{"9999999999999999999999.0.0", "999999999999999999999.0.0", true},
	} {
		if got := Newer(c.a, c.b); got != c.want {
			t.Errorf("%s > %s: %v", c.a, c.b, got)
		}
	}
}

func registryFixture(entries map[string]string) Runtime {
	r := New()
	r.http = &http.Client{Transport: roundTrip(func(req *http.Request) (*http.Response, error) {
		body, ok := entries[req.URL.Path]
		status := 200
		if !ok {
			status = 404
		}
		if body == "unavailable" {
			return nil, errors.New("offline")
		}
		return &http.Response{StatusCode: status, Body: io.NopCloser(strings.NewReader(body)), Header: make(http.Header), Request: req}, nil
	})}
	return r
}

func TestSupportedVersion(t *testing.T) {
	for _, c := range []struct {
		version   string
		supported bool
	}{
		{"0.162.999", false}, {"0.163.0", true}, {"0.164.0", true},
		{"0.163.0-beta", false}, {"invalid", false}, {"1.0.0", true},
	} {
		if SupportedVersion(c.version) != c.supported {
			t.Fatal(c)
		}
	}
}

func TestLatestHint(t *testing.T) {
	for _, body := range []string{`{"name":"@openai/codex","version":"0.163.0"}`, `{"name":"other","version":"0.163.0"}`, `{"name":"@openai/codex","version":"0.163.0-beta"}`, "unavailable", `{`} {
		r := registryFixture(map[string]string{"/@openai/codex/latest": body})
		got, err := r.Latest(context.Background())
		good := body == `{"name":"@openai/codex","version":"0.163.0"}`
		if (err == nil) != good || (good && got != "0.163.0") {
			t.Errorf("%s: %q %v", body, got, err)
		}
	}
}

func TestPlatformArtifacts(t *testing.T) {
	for _, target := range []struct{ goos, cpu, triple, arch string }{
		{"darwin", "arm64", "aarch64-apple-darwin", "arm64"},
		{"linux", "arm64", "aarch64-unknown-linux-musl", "arm64"},
		{"linux", "x64", "x86_64-unknown-linux-musl", "amd64"},
	} {
		for _, mode := range []string{"direct", "alias", "missing", "version", "alias_package", "alias_version", "alias_target", "main_integrity", "platform_integrity", "os", "cpu", "entry"} {
			t.Run(target.goos+"-"+target.cpu+"/"+mode, func(t *testing.T) {
				triple := target.triple
				if mode == "entry" {
					triple = "wrong-target"
				}
				r, data := platformFixture(t, target.goos, target.cpu, triple, mode != "direct")
				mainURL := "https://registry.npmjs.org/@openai%2Fcodex/0.163.0"
				var main map[string]any
				if err := json.Unmarshal(data[mainURL], &main); err != nil {
					t.Fatal(err)
				}
				dependencies := main["optionalDependencies"].(map[string]any)
				key := "@openai/codex-" + target.goos + "-" + target.cpu
				switch mode {
				case "missing":
					delete(dependencies, key)
				case "version":
					dependencies[key] = "0.164.0"
				case "alias_package":
					dependencies[key] = "npm:@evil/codex@0.163.0-" + target.goos + "-" + target.cpu
				case "alias_version":
					dependencies[key] = "npm:@openai/codex@0.164.0-" + target.goos + "-" + target.cpu
				case "alias_target":
					dependencies[key] = "npm:@openai/codex@0.163.0-other-" + target.cpu
				case "main_integrity":
					data["https://registry.npmjs.org/main.tgz"] = []byte("corrupt")
				case "platform_integrity":
					data["https://registry.npmjs.org/native.tgz"] = []byte("corrupt")
				}
				data[mainURL], _ = json.Marshal(main)
				if mode == "os" || mode == "cpu" {
					platformURL := mainURL + "-" + target.goos + "-" + target.cpu
					var platform map[string]any
					if err := json.Unmarshal(data[platformURL], &platform); err != nil {
						t.Fatal(err)
					}
					platform[mode] = []string{"other"}
					data[platformURL], _ = json.Marshal(platform)
				}
				var got []byte
				var err error
				if target.goos == "darwin" {
					var installed Installation
					installed, err = r.Install(context.Background(), "0.163.0", false)
					if err == nil {
						got, err = os.ReadFile(installed.Path)
					}
				} else {
					got, err = r.Linux(context.Background(), "0.163.0", target.arch)
					if err == nil {
						if !bytes.Equal(got, data["https://registry.npmjs.org/native.tgz"]) {
							t.Fatal("Linux runtime lost bundled files")
						}
						got, err = readEntryBounded(got, "package/vendor/"+target.triple+"/bin/codex", maxExpanded)
					}
				}
				if mode == "direct" || mode == "alias" {
					if err != nil || !bytes.Equal(got, []byte("xxxxxx")) {
						t.Fatalf("%q %v", got, err)
					}
				} else if err == nil {
					t.Fatal("invalid platform artifact accepted")
				}
			})
		}
	}
}

func TestLinuxTargetGuards(t *testing.T) {
	for _, c := range [][2]string{{"0.163.0", "ppc64"}, {"latest", "arm64"}} {
		if _, err := New().Linux(context.Background(), c[0], c[1]); err == nil {
			t.Fatal("invalid target accepted")
		}
	}
}

func TestLinuxRequiresCompanionFromSameVerifiedPackage(t *testing.T) {
	for _, mode := range []string{"missing", "empty", "not_executable", "symlink"} {
		t.Run(mode, func(t *testing.T) {
			r, data := platformFixture(t, "linux", "x64", "x86_64-unknown-linux-musl", true)
			prefix := "package/vendor/x86_64-unknown-linux-musl/bin/"
			entries := []*tar.Header{{Name: prefix + "codex", Typeflag: tar.TypeReg, Mode: 0755, Size: 6}}
			host := &tar.Header{Name: prefix + "codex-code-mode-host", Typeflag: tar.TypeReg, Mode: 0755, Size: 4}
			switch mode {
			case "empty":
				host.Size = 0
			case "not_executable":
				host.Mode = 0644
			case "symlink":
				host.Typeflag, host.Size, host.Linkname = tar.TypeSymlink, 0, "codex"
			}
			if mode != "missing" {
				entries = append(entries, host)
			}
			body := archive(t, entries...)
			data["https://registry.npmjs.org/native.tgz"] = body
			key := "https://registry.npmjs.org/@openai%2Fcodex/0.163.0-linux-x64"
			var metadata map[string]any
			if err := json.Unmarshal(data[key], &metadata); err != nil {
				t.Fatal(err)
			}
			sum := sha512.Sum512(body)
			metadata["dist"].(map[string]any)["integrity"] = "sha512-" + base64.StdEncoding.EncodeToString(sum[:])
			data[key], _ = json.Marshal(metadata)
			if _, err := r.Linux(context.Background(), "0.163.0", "amd64"); err == nil {
				t.Fatal("incomplete runtime accepted")
			}
		})
	}
}
