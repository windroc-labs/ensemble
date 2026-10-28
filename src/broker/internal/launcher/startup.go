package launcher

import (
	"context"
	"errors"

	"ensemble/broker/internal/docker"
	"ensemble/broker/internal/protocol"
)

func (h Host) bootstrap(ctx context.Context, c string, m Manifest) error {
	script, err := checks.ReadFile("checks/start-container.py")
	if err != nil {
		return protocol.New(protocol.StartFailed, "Embedded image checks unreadable.")
	}
	content, err := checks.ReadFile("checks/image-content-check")
	if err != nil {
		return protocol.New(protocol.StartFailed, "Embedded image checks unreadable.")
	}
	user, hook := "agent", ""
	if m.HasHook {
		user, hook = "root", m.HookPath
	}
	_, err = h.Docker.Run(ctx, []string{"exec", "--interactive", "--user", user, c, "/usr/bin/python3", "-I", "-",
		m.Client, m.StateDir(), m.CWD, hook, string(content)}, script)
	if err == nil {
		return nil
	}
	message := "Image content under /opt/ensemble is unreadable by agent; rebuild with ensemble-broker build."
	var ce *docker.CommandError
	if errors.As(err, &ce) {
		switch ce.Exit {
		case 42:
			message = "Writing client bootstrap state failed."
		case 43:
			message = "Installing the hook stub failed."
		}
	}
	return failure(err, protocol.StartFailed, message)
}
