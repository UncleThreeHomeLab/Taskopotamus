{{- define "taskopotamus.validate" -}}
{{- if has .Release.Namespace (list "default" "kube-system" "kube-public" "kube-node-lease") -}}
{{- fail "Use a dedicated Taskopotamus namespace, not a default or Kubernetes system namespace." -}}
{{- end -}}
{{- $ips := list .Values.network.agentServiceIP .Values.network.mcpServiceIP .Values.network.dnsIP -}}
{{- if ne (len (uniq $ips)) 3 -}}{{- fail "Agent, MCP, and DNS IPv4 addresses must be distinct." -}}{{- end -}}
{{- if gt (float64 .Values.agent.artifacts.stagingSeconds) (float64 .Values.agent.artifacts.retentionSeconds) -}}
{{- fail "Artifact stagingSeconds must not exceed retentionSeconds." -}}
{{- end -}}
{{- $claims := list -}}
{{- $names := list "postgres" "artifacts" "google" "native" "workspace" "saved" "state" -}}
{{- if .Values.objects.enabled -}}{{- $names = append $names "objects" -}}{{- end -}}
{{- range $name := $names -}}{{- $claims = append $claims (include "taskopotamus.claim" (dict "root" $ "name" $name)) -}}{{- end -}}
{{- if .Values.storage.legacyClaim -}}{{- $claims = append $claims .Values.storage.legacyClaim -}}{{- end -}}
{{- if ne (len (uniq $claims)) (len $claims) -}}{{- fail "Data, native, scratch, saved, supervisor, and legacy claim names must be distinct." -}}{{- end -}}
{{- end -}}

{{- define "taskopotamus.components" -}}
{{- $names := list "postgres" "agent" "mcp" "google" -}}
{{- if .Values.telegram.enabled -}}{{- $names = append $names "telegram" -}}{{- end -}}
{{- $names = append $names "worker" -}}
{{- if .Values.objects.enabled -}}{{- $names = append $names "objects" -}}{{- end -}}
{{- if .Values.console.enabled -}}{{- $names = append $names "test-console" -}}{{- end -}}
{{- toJson $names -}}
{{- end -}}

{{- define "taskopotamus.labels" -}}
app.kubernetes.io/name: taskopotamus
app.kubernetes.io/instance: {{ .root.Release.Name | quote }}
app.kubernetes.io/component: {{ .component | quote }}
{{- end -}}

{{- define "taskopotamus.image" -}}
{{- $component := .component -}}{{- if eq $component "test-console" -}}{{- $component = "agent" -}}{{- end -}}
{{- $image := index .root.Values.images $component -}}
{{- if $image.digest -}}
{{- printf "%s@%s" $image.repository $image.digest -}}
{{- else -}}
{{- printf "%s:%s" $image.repository $image.tag -}}
{{- end -}}
{{- end -}}

{{- define "taskopotamus.claim" -}}
{{- default (printf "%s-%s" .root.Release.Name .name) (index .root.Values.storage.existingClaims .name) -}}
{{- end -}}

{{- define "taskopotamus.secrets" -}}
{{- $keys := dict -}}
{{- if eq .component "postgres" -}}
{{- $keys = dict
  "postgres_admin_password" "postgres_admin_password"
  "agent_database_password" "agent_database_password"
  "profile_database_password" "profile_database_password"
  "telegram_database_password" "telegram_database_password" -}}
{{- else if eq .component "agent" -}}
{{- $keys = dict
  "agent_api_token" "agent_api_token"
  "profile_signing_key" "profile_signing_key"
  "google_data_secret" "google_data_secret"
  "database_password" "agent_database_password"
  "worker_client_key" "worker_client_key"
  "worker_host_public" "worker_host_key.pub" -}}
{{- else if eq .component "mcp" -}}
{{- $keys = dict "profile_signing_key" "profile_signing_key" "database_password" "profile_database_password" -}}
{{- else if eq .component "google" -}}
{{- $keys = dict "google_control_secret" "google_control_secret" "google_data_secret" "google_data_secret" "google_token_encryption_key" "google_token_encryption_key" -}}
{{- if .root.Values.google.oauthEnabled -}}
{{- $_ := set $keys "google_oauth_client_id" "google_oauth_client_id" -}}
{{- $_ := set $keys "google_oauth_client_secret" "google_oauth_client_secret" -}}
{{- $_ := set $keys "google_oauth_redirect_uri" "google_oauth_redirect_uri" -}}
{{- end -}}
{{- else if eq .component "telegram" -}}
{{- $keys = dict "agent_api_token" "agent_api_token" "google_control_secret" "google_control_secret" "database_password" "telegram_database_password" -}}
{{- else if eq .component "worker" -}}
{{- $keys = dict "worker_host_key" "worker_host_key" "worker_client_public" "worker_client_key.pub" -}}
{{- else if eq .component "objects" -}}
{{- $keys = dict "silo_root_password" "silo_root_password" -}}
{{- else if eq .component "test-console" -}}
{{- $keys = dict "agent_api_token" "agent_api_token" -}}
{{- end -}}
{{- toJson $keys -}}
{{- end -}}

{{- define "taskopotamus.data" -}}
{{- $data := dict -}}
{{- if eq .component "postgres" -}}{{- $data = dict "postgres" "/var/lib/postgresql" -}}
{{- else if eq .component "agent" -}}{{- $data = dict "artifacts" "/var/lib/taskopotamus/artifacts" -}}
{{- else if eq .component "google" -}}{{- $data = dict "google" "/var/lib/taskopotamus/google" -}}
{{- else if eq .component "worker" -}}{{- $data = dict "native" "/home/agent/.codex" "workspace" "/workspace" "saved" "/saved" "state" "/var/lib/taskopotamus" -}}
{{- else if eq .component "objects" -}}{{- $data = dict "objects" "/data" -}}
{{- end -}}
{{- toJson $data -}}
{{- end -}}

{{- define "taskopotamus.environment" -}}
{{- $v := .root.Values -}}{{- $env := dict -}}
{{- if has .component (list "agent" "mcp" "telegram") -}}
{{- $database := index (dict "agent" "agent" "mcp" "profiles" "telegram" "telegram") .component -}}
{{- $env = dict
  "DATABASE_HOST" "postgres"
  "DATABASE_PORT" "5432"
  "DATABASE_SSLMODE" "disable"
  "DATABASE_NAME" (printf "taskopotamus_%s" $database)
  "DATABASE_USER" (printf "taskopotamus_%s" $database)
  "DATABASE_PASSWORD_FILE" "/run/secrets/database_password" -}}
{{- end -}}
{{- if eq .component "postgres" -}}
{{- $env = dict
  "POSTGRES_USER" "postgres"
  "POSTGRES_PASSWORD_FILE" "/run/secrets/postgres_admin_password"
  "POSTGRES_INITDB_ARGS" "--auth-host=scram-sha-256"
  "PGDATA" "/var/lib/postgresql/18/docker" -}}
{{- else if eq .component "agent" -}}
{{- range $name, $value := dict
  "CODEX_MODEL" $v.agent.model
  "CODEX_REASONING_EFFORT" $v.agent.reasoningEffort
  "CODEX_AUTO_COMPACT_PERCENT" $v.agent.autoCompactPercent
  "CODEX_SUBAGENT_MODEL" $v.agent.subagentModel
  "CODEX_SUBAGENT_REASONING_EFFORT" $v.agent.subagentReasoningEffort
  "CODEX_SUBAGENT_MAX_THREADS" $v.agent.subagentMaxThreads
  "CODEX_TURN_TIMEOUT_SECONDS" $v.agent.turnTimeoutSeconds
  "CODEX_BIN" "/usr/local/bin/codex-worker"
  "WORKER_AGENT_IP" $v.network.agentServiceIP
  "WORKER_CLIENT_KEY_FILE" "/run/secrets/worker_client_key"
  "WORKER_HOST_PUBLIC_KEY_FILE" "/run/secrets/worker_host_public"
  "AGENT_API_TOKEN_FILE" "/run/secrets/agent_api_token"
  "PROFILE_SERVICE_URL" "http://mcp:8080"
  "GOOGLE_DATA_SECRET_FILE" "/run/secrets/google_data_secret"
  "PROFILE_SIGNING_KEY_FILE" "/run/secrets/profile_signing_key"
  "ARTIFACT_DIRECTORY" "/var/lib/taskopotamus/artifacts" -}}
{{- $_ := set $env $name $value -}}
{{- end -}}
{{- range $key, $name := dict
  "maxFiles" "ARTIFACT_MAX_FILES"
  "inputMaxBytes" "ARTIFACT_INPUT_MAX_BYTES"
  "outputMaxBytes" "ARTIFACT_OUTPUT_MAX_BYTES"
  "imageMaxBytes" "ARTIFACT_IMAGE_MAX_BYTES"
  "capacityBytes" "ARTIFACT_CAPACITY_BYTES"
  "stagingSeconds" "ARTIFACT_STAGING_SECONDS"
  "retentionSeconds" "ARTIFACT_RETENTION_SECONDS"
  "sweepSeconds" "ARTIFACT_SWEEP_SECONDS" -}}
{{- $_ := set $env $name (index $v.agent.artifacts $key) -}}
{{- end -}}
{{- else if eq .component "mcp" -}}
{{- $_ := set $env "PROFILE_SIGNING_KEY_FILE" "/run/secrets/profile_signing_key" -}}
{{- else if eq .component "telegram" -}}
{{- range $name, $value := dict
  "TELEGRAM_ALLOWED_USER_IDS" (join "," $v.telegram.allowedUserIds)
  "AGENT_URL" "http://agent:8080"
  "AGENT_API_TOKEN_FILE" "/run/secrets/agent_api_token"
  "CODEX_TURN_TIMEOUT_SECONDS" $v.agent.turnTimeoutSeconds
  "GOOGLE_SERVICE_URL" "http://google:8081"
  "GOOGLE_CONTROL_SECRET_FILE" "/run/secrets/google_control_secret" -}}{{- $_ := set $env $name $value -}}{{- end -}}
{{- else if eq .component "google" -}}
{{- $env = dict
  "GOOGLE_CONTROL_SECRET_FILE" "/run/secrets/google_control_secret"
  "GOOGLE_TOKEN_ENCRYPTION_KEY_FILE" "/run/secrets/google_token_encryption_key"
  "GOOGLE_DATA_SECRET_FILE" "/run/secrets/google_data_secret"
  "GOOGLE_STORAGE_DIRECTORY" "/var/lib/taskopotamus/google" -}}
{{- if $v.google.oauthEnabled -}}
{{- range $name := list "CLIENT_ID" "CLIENT_SECRET" "REDIRECT_URI" -}}{{- $_ := set $env (printf "GOOGLE_OAUTH_%s_FILE" $name) (printf "/run/secrets/google_oauth_%s" (lower $name)) -}}{{- end -}}
{{- end -}}
{{- else if eq .component "worker" -}}
{{- $env = dict
  "WORKER_AGENT_IP" $v.network.agentServiceIP
  "WORKER_MCP_IP" $v.network.mcpServiceIP
  "WORKER_DNS_IP" $v.network.dnsIP
  "WORKSPACE_RETENTION_SECONDS" $v.worker.retentionSeconds
  "WORKSPACE_SWEEP_SECONDS" $v.worker.sweepSeconds
  "WORKSPACE_MAX_BYTES" $v.worker.maxBytes -}}
{{- else if eq .component "objects" -}}
{{- $env = dict "MINIO_ROOT_USER" "taskopotamus-admin" "MINIO_ROOT_PASSWORD_FILE" "/run/secrets/silo_root_password" "MINIO_BROWSER" "off" "MINIO_UPDATE" "off" -}}
{{- else if eq .component "test-console" -}}
{{- $env = dict "AGENT_URL" "http://agent:8080" "AGENT_API_TOKEN_FILE" "/run/secrets/agent_api_token" "CODEX_TURN_TIMEOUT_SECONDS" $v.agent.turnTimeoutSeconds -}}
{{- end -}}
{{- toJson $env -}}
{{- end -}}
