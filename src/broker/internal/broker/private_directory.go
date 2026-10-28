package broker

import (
	"errors"
	"os"
	"path/filepath"
	"syscall"
)

var errPrivateDirectory = errors.New("private broker state directory unavailable")

func privateDirectory(directory string, create bool) (string, error) {
	if !filepath.IsAbs(directory) || filepath.Clean(directory) != directory {
		return "", errPrivateDirectory
	}
	var parts []string
	for p := directory; p != filepath.Dir(p); p = filepath.Dir(p) {
		parts = append(parts, p)
	}
	for i := len(parts) - 1; i >= 0; i-- {
		p := parts[i]
		fi, err := os.Lstat(p)
		if os.IsNotExist(err) && create {
			err = os.Mkdir(p, 0o700)
			if os.IsExist(err) {
				err = nil
			}
			if err == nil {
				fi, err = os.Lstat(p)
			}
		}
		if os.IsNotExist(err) && !create {
			return directory, nil
		}
		if err != nil || !fi.IsDir() || fi.Mode()&os.ModeSymlink != 0 {
			return "", errPrivateDirectory
		}
		if i == 0 {
			s, ok := fi.Sys().(*syscall.Stat_t)
			if !ok || s.Uid != uint32(os.Geteuid()) || fi.Mode().Perm()&0o077 != 0 {
				return "", errPrivateDirectory
			}
		}
	}
	return directory, nil
}
