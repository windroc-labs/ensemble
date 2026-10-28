package credential

// LinearApp is a role app reference, matched against the broker's enrollment.
type LinearApp struct {
	Role          string `json:"role"`
	ClientID      string `json:"client_id"`
	UserID        string `json:"user_id"`
	SecretService string `json:"secret_service"`
	TokenService  string `json:"token_service"`
}

// LinearBoard contains only the non-secret board settings a run can use.
type LinearBoard struct {
	Team   string            `json:"team"`
	States map[string]string `json:"states"`
	Labels struct {
		TaskLabels  map[string]string `json:"task_labels"`
		AgentLabels map[string]string `json:"agent_labels"`
	} `json:"labels"`
	ProjectRepos map[string]string `json:"project_repos"`
	HumanMembers []string          `json:"human_members"`
}

type LinearRequest struct {
	App   LinearApp   `json:"app"`
	Board LinearBoard `json:"board"`
}

const LinearDir = "/run/ensemble/secrets/linear"
