package credential

import "strings"

// Redact replaces each non-empty secret in text with [redacted].
func Redact(text string, secrets ...string) string {
	for _, s := range secrets {
		if s != "" {
			text = strings.ReplaceAll(text, s, "[redacted]")
		}
	}
	return text
}
