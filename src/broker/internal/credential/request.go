package credential

import "regexp"

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
	RoleUsers       map[string]string `json:"role_users"`
	TaskLabelGroup  string            `json:"task_label_group"`
	AgentLabelGroup string            `json:"agent_label_group"`
	Team            string            `json:"team"`
	States          map[string]string `json:"states"`
	Labels          struct {
		TaskLabels  map[string]string `json:"task_labels"`
		AgentLabels map[string]string `json:"agent_labels"`
	} `json:"labels"`
	ProjectRepos map[string]string `json:"project_repos"`
	HumanMembers []string          `json:"human_members"`
}

type LinearRequest struct {
	Scopes string      `json:"scopes"`
	App    LinearApp   `json:"app"`
	Board  LinearBoard `json:"board"`
}

const LinearRoleScopes = "read,write,app:assignable,app:mentionable"

func ValidLinearScopes(s string) bool { return s == LinearRoleScopes }

const LinearDir = "/run/ensemble/secrets/linear"

var linearRolePattern = regexp.MustCompile(`^[a-z][a-z0-9-]{0,62}$`)
var linearServicePattern = regexp.MustCompile(`^[A-Za-z0-9._@+-]{1,128}$`)
var linearUserPattern = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)

func ValidLinearRole(s string) bool { return linearRolePattern.MatchString(s) }
func ValidLinearService(s string) bool {
	return linearServicePattern.MatchString(s) && s != "." && s != ".."
}
func ValidLinearUser(s string) bool { return linearUserPattern.MatchString(s) }
