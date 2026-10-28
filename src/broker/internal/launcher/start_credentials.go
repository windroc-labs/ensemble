package launcher

import (
	"context"
	"encoding/json"
	"slices"

	"ensemble/broker/internal/protocol"
)

const credentialBatch = "/opt/ensemble/lib/credential-batch.py"

type startCommand struct {
	Argv  []string `json:"argv"`
	Stdin []byte   `json:"stdin"`
}

type deliveryStep struct {
	end     int
	message string
	confirm func()
}

// startDelivery stages provider writes without changing the synchronous transport used by refresh.
type startDelivery struct {
	commands []startCommand
	steps    []deliveryStep
}

func (b *startDelivery) queue(argv []string, stdin []byte) error {
	b.commands = append(b.commands, startCommand{Argv: slices.Clone(argv), Stdin: slices.Clone(stdin)})
	return nil
}

func (b *startDelivery) step(message string, confirm func()) {
	b.steps = append(b.steps, deliveryStep{end: len(b.commands), message: message, confirm: confirm})
}

func (b *startDelivery) erase() {
	for _, command := range b.commands {
		clear(command.Stdin)
	}
}

func (h Host) deliverStart(ctx context.Context, c string, b *startDelivery) error {
	completed := 0
	if len(b.commands) > 0 {
		data, err := json.Marshal(b.commands)
		if err != nil {
			return protocol.New(protocol.StartFailed, "Writing startup credentials failed.")
		}
		defer clear(data)
		out, err := h.Docker.Run(ctx, []string{"exec", "--interactive", "--user", "agent", c, Python, "-I", credentialBatch}, data)
		if err != nil {
			return injected(err, protocol.StartFailed, "Writing startup credentials failed.")
		}
		var result struct {
			Completed *int `json:"completed"`
		}
		if json.Unmarshal(out, &result) != nil || result.Completed == nil || *result.Completed < 0 || *result.Completed > len(b.commands) {
			return protocol.New(protocol.StartFailed, "Writing startup credentials failed.")
		}
		completed = *result.Completed
	}
	for _, step := range b.steps {
		if step.end > completed {
			return protocol.New(protocol.StartFailed, step.message)
		}
		if step.confirm != nil {
			step.confirm()
		}
	}
	return nil
}
