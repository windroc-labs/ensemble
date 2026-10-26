package credential

import (
	"errors"
	"fmt"
	"strings"

	"ensemble/broker/internal/docker"
)

// ErrDelivery is a write into a container that failed, or a file name refused before it: never Docker's stderr.
var ErrDelivery = errors.New("writing into the container failed")

// Deliver writes value to f through exec's stdin, never its argv: 0400 in a 0700 directory. A create-only file
// refuses one already there (set -C); Replace writes a dot file, which no reader matches, then renames it over.
// Errors wrap docker.ErrUnreachable or are ErrDelivery.
func Deliver(exec func(argv []string, stdin []byte) error, f File, value []byte) error {
	if f.Name == "" || strings.Contains(f.Name, "/") || strings.HasPrefix(f.Name, ".") {
		return ErrDelivery
	}
	script := "mkdir -p -m 0700 " + f.Dir + " && umask 0377 && set -C && cat > " + f.Dir + `/"$1"`
	if f.Replace {
		temporary := f.Dir + `/."$1".new`
		script = "mkdir -p -m 0700 " + f.Dir + " && umask 0377 && rm -f " + temporary + " && cat > " + temporary +
			" && mv -f " + temporary + " " + f.Dir + `/"$1"`
	}
	err := exec([]string{"/bin/sh", "-c", script, "sh", f.Name}, value)
	switch {
	case errors.Is(err, docker.ErrUnreachable):
		return fmt.Errorf("credential delivery: %w", docker.ErrUnreachable)
	case err != nil:
		return ErrDelivery
	}
	return nil
}
