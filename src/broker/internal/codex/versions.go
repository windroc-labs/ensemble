package codex

import "strings"

const MinVersion = "0.163.0"

func SupportedVersion(version string) bool {
	return ValidVersion(version) && !Newer(MinVersion, version)
}

func Newer(a, b string) bool {
	if !ValidVersion(a) || !ValidVersion(b) {
		return false
	}
	x, y := strings.Split(a, "."), strings.Split(b, ".")
	for i := range x {
		if len(x[i]) != len(y[i]) {
			return len(x[i]) > len(y[i])
		}
		if x[i] != y[i] {
			return x[i] > y[i]
		}
	}
	return false
}
