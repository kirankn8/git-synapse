{{- define "git-synapse.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "git-synapse.fullname" -}}
{{- if .Values.fullnameOverride }}{{ .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}{{ else }}{{ include "git-synapse.name" . }}{{ end }}
{{- end }}

{{- define "git-synapse.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ include "git-synapse.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "git-synapse.selectorLabels" -}}
app.kubernetes.io/name: {{ include "git-synapse.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "git-synapse.secretName" -}}
{{- default (include "git-synapse.fullname" .) .Values.secrets.existingSecret }}
{{- end }}

{{- define "git-synapse.postgresSecretName" -}}
{{- if .Values.postgresql.enabled }}{{ include "git-synapse.fullname" . }}-postgres{{ else }}{{ default (include "git-synapse.fullname" .) .Values.externalDatabase.existingSecret }}{{ end }}
{{- end }}

{{- define "git-synapse.dbHost" -}}
{{- if .Values.postgresql.enabled }}{{ include "git-synapse.fullname" . }}-postgres{{ else }}{{ required "externalDatabase.host is required when postgresql.enabled=false" .Values.externalDatabase.host }}{{ end }}
{{- end }}

{{- define "git-synapse.dbPort" -}}
{{- if .Values.postgresql.enabled }}5432{{ else }}{{ .Values.externalDatabase.port }}{{ end }}
{{- end }}

{{- define "git-synapse.dbName" -}}
{{- if .Values.postgresql.enabled }}{{ .Values.postgresql.database }}{{ else }}{{ .Values.externalDatabase.database }}{{ end }}
{{- end }}

{{- define "git-synapse.dbUser" -}}
{{- if .Values.postgresql.enabled }}{{ .Values.postgresql.username }}{{ else }}{{ .Values.externalDatabase.username }}{{ end }}
{{- end }}

{{- define "git-synapse.env" -}}
- name: POSTGRES_HOST
  value: {{ include "git-synapse.dbHost" . | quote }}
- name: POSTGRES_PORT
  value: {{ include "git-synapse.dbPort" . | quote }}
- name: POSTGRES_DB
  value: {{ include "git-synapse.dbName" . | quote }}
- name: POSTGRES_USER
  value: {{ include "git-synapse.dbUser" . | quote }}
- name: POSTGRES_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "git-synapse.postgresSecretName" . }}
      key: POSTGRES_PASSWORD
- name: ADMIN_SETUP_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ include "git-synapse.secretName" . }}
      key: ADMIN_SETUP_TOKEN
- name: GS_SECRET_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "git-synapse.secretName" . }}
      key: GS_SECRET_KEY
- name: GITHUB_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ include "git-synapse.secretName" . }}
      key: GITHUB_TOKEN
      optional: true
- name: INGEST_CONCURRENCY
  value: {{ .Values.config.ingestConcurrency | quote }}
- name: MAX_FILES_PER_COMMIT
  value: {{ .Values.config.maxFilesPerCommit | quote }}
- name: MIN_PAIR_SUPPORT
  value: {{ .Values.config.minPairSupport | quote }}
- name: REFRESH_CRON
  value: {{ .Values.config.refreshCron | quote }}
- name: DISCOVER_CRON
  value: {{ .Values.config.discoverCron | quote }}
- name: SCHEDULER_TZ
  value: {{ .Values.config.schedulerTimezone | quote }}
- name: LOG_LEVEL
  value: {{ .Values.config.logLevel | quote }}
- name: MIRROR_ROOT
  value: /data/mirrors
{{- end }}
