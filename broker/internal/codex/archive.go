package codex

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"errors"
	"io"
	"path"
	"strings"
)

func readEntryBounded(b []byte, entry string, limit int64) ([]byte, error) {
	z, err := gzip.NewReader(bytes.NewReader(b))
	if err != nil {
		return nil, errors.New("invalid Codex gzip archive")
	}
	defer z.Close()
	expanded := &io.LimitedReader{R: z, N: limit + 1}
	tr := tar.NewReader(expanded)
	seen := map[string]bool{}
	var executable []byte
	for count := 0; ; count++ {
		h, err := tr.Next()
		if err == io.EOF {
			break
		}
		if err != nil || count >= 4096 {
			return nil, errors.New("invalid Codex tar archive")
		}
		name := strings.TrimSuffix(h.Name, "/")
		if name == "" || strings.ContainsAny(name, "\\\x00") || path.Clean(name) != name || (name != "package" && !strings.HasPrefix(name, "package/")) || seen[name] || h.Size < 0 || h.Size > limit || h.Mode&07000 != 0 {
			return nil, errors.New("unsafe Codex archive entry")
		}
		seen[name] = true
		if h.Typeflag != tar.TypeReg && h.Typeflag != tar.TypeDir {
			return nil, errors.New("Codex archive links and special files rejected")
		}
		if h.Typeflag == tar.TypeDir {
			if h.Size != 0 {
				return nil, errors.New("invalid Codex archive directory")
			}
			continue
		}
		if name == entry && entry != "" {
			if h.Size == 0 || h.Mode&0111 == 0 {
				return nil, errors.New("Codex native executable is empty or not executable")
			}
			executable, err = io.ReadAll(tr)
			if err != nil {
				return nil, errors.New("cannot read Codex native executable")
			}
		}
	}
	if _, err := io.Copy(io.Discard, expanded); err != nil || expanded.N == 0 {
		return nil, errors.New("Codex expanded archive exceeds limit or is corrupt")
	}
	if entry != "" && executable == nil {
		return nil, errors.New("Codex native executable missing")
	}
	return executable, nil
}
