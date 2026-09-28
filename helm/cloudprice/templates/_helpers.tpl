{{- define "cloudprice.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "cloudprice.fullname" -}}
{{- printf "%s-%s" .Release.Name (include "cloudprice.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "cloudprice.labels" -}}
app.kubernetes.io/name: {{ include "cloudprice.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Values.image.tag | default .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "cloudprice.selectorLabels" -}}
app.kubernetes.io/name: {{ include "cloudprice.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
