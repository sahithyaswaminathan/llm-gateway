{{- define "ollama.fullname" -}}
{{- printf "%s-ollama" .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- define "ollama.labels" -}}
app.kubernetes.io/name: ollama
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
