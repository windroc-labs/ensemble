package credential

import (
	"errors"
	"fmt"
	"slices"
	"strings"

	"ensemble/broker/internal/docker"
)

// ErrDelivery is a write into a container that failed, or a file name refused before it: never Docker's stderr.
var ErrDelivery = errors.New("writing into the container failed")

// Command transfers a credential write and its owned stdin buffer to the caller.
type Command struct {
	Argv  []string `json:"argv"`
	Stdin []byte   `json:"stdin"`
}

func (c Command) Erase() { clear(c.Stdin) }

// Deliver writes value to f through exec's stdin, never its argv: 0400 in a 0700 directory. A create-only file
// refuses one already there (set -C); Replace writes a dot file, which no reader matches, then renames it over.
// Name must be one path component; a Replace one must not start with a dot, the temporary names'. Errors wrap
// docker.ErrUnreachable or are ErrDelivery.
func Deliver(exec func(argv []string, stdin []byte) error, f File, value []byte) error {
	command, err := Prepare(f, value)
	if err != nil {
		return err
	}
	defer command.Erase()
	err = exec(command.Argv, command.Stdin)
	switch {
	case errors.Is(err, docker.ErrUnreachable):
		return fmt.Errorf("credential delivery: %w", docker.ErrUnreachable)
	case err != nil:
		return ErrDelivery
	}
	return nil
}

// Prepare builds the same write as Deliver, copying value; the caller executes and erases it.
func Prepare(f File, value []byte) (Command, error) {
	if f.Name == "" || f.Name == "." || f.Name == ".." || strings.Contains(f.Name, "/") || f.Replace && strings.HasPrefix(f.Name, ".") {
		return Command{}, ErrDelivery
	}
	script := "mkdir -p -m 0700 " + f.Dir + " && umask 0377 && set -C && cat > " + f.Dir + `/"$1"`
	if f.Replace {
		temporary := f.Dir + `/."$1".new`
		script = "mkdir -p -m 0700 " + f.Dir + " && umask 0377 && rm -f " + temporary + " && cat > " + temporary +
			" && mv -f " + temporary + " " + f.Dir + `/"$1"`
	}
	return Command{Argv: []string{"/bin/sh", "-c", script, "sh", f.Name}, Stdin: slices.Clone(value)}, nil
}
