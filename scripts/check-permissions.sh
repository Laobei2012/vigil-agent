#!/usr/bin/env bash
# Checks that the vigil-agent Kubernetes identity is read-only.
# It uses SubjectAccessReview, so it works where impersonation (--as) is denied,
# for example through Teleport.
# Usage: scripts/check-permissions.sh <kube-context> [namespace] [release]
# Exits 1 if the ServiceAccount can read secrets, exec into pods, or write anything.
set -euo pipefail

context="${1:?usage: $0 <kube-context> [namespace] [release]}"
namespace="${2:-vigil-agent}"
release="${3:-vigil-agent}"
sa_name="${release}-k8s-reader"
user="system:serviceaccount:${namespace}:${sa_name}"
failures=0

# can <verb> <resource[.group][/subresource]> [namespace]
can() {
  local verb="$1" spec="$2" ns="${3:-}"
  local resource="${spec%%/*}" sub="" group=""
  [[ $spec == */* ]] && sub="${spec#*/}"
  if [[ $resource == *.* ]]; then
    group="${resource#*.}"
    resource="${resource%%.*}"
  fi
  kubectl --context "$context" create -o jsonpath='{.status.allowed}' -f - << EOF_SAR
apiVersion: authorization.k8s.io/v1
kind: SubjectAccessReview
spec:
  user: "${user}"
  groups: ["system:serviceaccounts", "system:serviceaccounts:${namespace}", "system:authenticated"]
  resourceAttributes:
    verb: "${verb}"
    group: "${group}"
    resource: "${resource}"
    subresource: "${sub}"
    namespace: "${ns}"
EOF_SAR
}

expect() {
  local want="$1"
  shift
  local got
  got="$(can "$@")"
  [[ $got == "true" ]] && got=yes || got=no
  if [[ $got == "$want" ]]; then
    printf 'ok    %-3s %s\n' "$got" "$*"
  else
    printf 'FAIL  %-3s %s (want %s)\n' "$got" "$*" "$want"
    failures=$((failures + 1))
  fi
}

expect no get secrets
expect no list secrets
expect no watch secrets oci-cost
expect no create pods/exec oci-cost
expect no get pods/exec oci-cost
expect no create pods/attach oci-cost
expect no create pods/portforward oci-cost
expect no create serviceaccounts/token oci-cost
expect no delete pods oci-cost
expect no patch deployments.apps monitoring
expect no create jobs.batch oci-cost
expect no update configmaps monitoring
expect no delete helmreleases.helm.toolkit.fluxcd.io monitoring
expect no patch kustomizations.kustomize.toolkit.fluxcd.io flux-system
expect no update certificatesigningrequests.certificates.k8s.io/approval
expect yes get pods oci-cost
expect yes get pods/log oci-cost
expect yes list helmreleases.helm.toolkit.fluxcd.io
expect yes list volumes.longhorn.io longhorn
expect yes list certificatesigningrequests.certificates.k8s.io

# Every role bound to the ServiceAccount must grant only get, list, and watch,
# and never secrets.
bound="$(kubectl --context "$context" get clusterrolebindings,rolebindings -A -o json \
  | jq -r --arg ns "$namespace" --arg sa "$sa_name" '
      .items[]
      | select(any(.subjects[]?; .kind == "ServiceAccount" and .name == $sa and .namespace == $ns))
      | "\(.roleRef.kind) \(.roleRef.name) \(.metadata.namespace // "")"')"
while read -r kind name ns; do
  [[ -z $kind ]] && continue
  if [[ $kind == "ClusterRole" ]]; then
    rules="$(kubectl --context "$context" get clusterrole "$name" -o json)"
  else
    rules="$(kubectl --context "$context" -n "$ns" get role "$name" -o json)"
  fi
  bad="$(jq -r '.rules[]
      | select((.verbs - ["get", "list", "watch"] | length > 0)
          or (.resources // [] | any(. == "secrets" or . == "*")))
      | tostring' <<< "$rules")"
  if [[ -n $bad ]]; then
    printf 'FAIL  %s %s grants more than read access:\n%s\n' "$kind" "$name" "$bad"
    failures=$((failures + 1))
  else
    printf 'ok    %s %s grants only get, list, and watch, without secrets\n' "$kind" "$name"
  fi
done <<< "$bound"

if ((failures > 0)); then
  echo "$failures check(s) failed"
  exit 1
fi
echo "all permission checks passed"
