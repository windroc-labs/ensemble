package credential

import "strings"

// A static secret's reference, its safe name: the prefix of its kind, then its name. The broker package pins each
// prefix to its keychain service.
const (
	ClaudePrefix   = "ensemble-broker-claude/"
	OpencodePrefix = "ensemble-broker-opencode/"
	EnvPrefix      = "ensemble-broker-env/"
)

var staticPrefixes = map[Kind]string{Claude: ClaudePrefix, Opencode: OpencodePrefix, Env: EnvPrefix}

// StaticRef is the reference of kind k's secret name; a token kind has none.
func StaticRef(k Kind, name string) string {
	prefix, ok := staticPrefixes[k]
	if !ok {
		panic("not a static kind: " + string(k))
	}
	return prefix + name
}

// StaticKind is ref's kind and name; false for a string that is no static reference.
func StaticKind(ref string) (Kind, string, bool) {
	for k, prefix := range staticPrefixes {
		if name, ok := strings.CutPrefix(ref, prefix); ok && name != "" {
			return k, name, true
		}
	}
	return "", "", false
}

// LoginCommand is the broker command that saves ref's secret.
func LoginCommand(ref string) string {
	k, name, _ := StaticKind(ref)
	switch k {
	case Claude:
		return "ensemble-broker login claude " + name
	case Opencode:
		return "ensemble-broker login opencode " + name
	}
	return "ensemble-broker login env " + name
}
