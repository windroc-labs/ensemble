package launcher

import "ensemble/broker/internal/docker"

// unreachableErr is what the docker CLI returns for a target it cannot reach.
var unreachableErr = &docker.Unreachable{Endpoint: "ssh://u@pc", Reason: "host key mismatch"}
