package launcher

import (
	"context"
	_ "embed"
	"errors"

	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/protocol"
)

//go:embed startup/startup.py
var startupScript []byte

const (
	contentFailed   = 41
	bootstrapFailed = 42
)

func (h Host) bootstrap(ctx context.Context, c string, m Manifest) error {
	content, err := checks.ReadFile("checks/image-content-check")
	if err != nil {
		return protocol.New(protocol.StartFailed, "Embedded image checks unreadable.")
	}
	_, err = h.Docker.Run(ctx, []string{"exec", "--interactive", "--user", "agent", c, "/usr/bin/python3", "-I", "-",
		m.Client, m.StateDir(), m.CWD, string(content)}, startupScript)
	if err == nil {
		if m.HasHook {
			if _, err := h.Docker.Run(ctx, []string{"exec", "--user", "root", c, Python, "-I", Bootstrap, "hook", m.HookPath}, nil); err != nil {
				return failure(err, protocol.StartFailed, "Installing the hook stub failed.")
			}
		}
		return nil
	}
	message := "Container bootstrap failed."
	var ce *docker.CommandError
	if errors.As(err, &ce) {
		switch ce.Exit {
		case contentFailed:
			message = "Image content under /opt/ensemble is unreadable by agent; rebuild with ensemble-broker build."
		case bootstrapFailed:
			message = "Writing client bootstrap state failed."
		}
	}
	return failure(err, protocol.StartFailed, message)
}
