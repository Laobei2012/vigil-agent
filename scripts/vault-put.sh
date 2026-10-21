#!/usr/bin/env bash
# Creates or updates one secret in the OCI Vault that the cluster's External
# Secrets store (ClusterSecretStore oracle-vault) reads.
# Usage: scripts/vault-put.sh <kube-context> <SECRET_NAME> < value-file
#    or: scripts/vault-put.sh <kube-context> <SECRET_NAME>   (hidden prompt)
# The value stays out of the shell history. The oci command line holds it in base64
# for a moment, so run this on a machine that other users cannot inspect.
set -euo pipefail

context="${1:?usage: $0 <kube-context> <SECRET_NAME>}"
name="${2:?usage: $0 <kube-context> <SECRET_NAME>}"
store="${STORE:-oracle-vault}"

vault="$(kubectl --context "$context" get clustersecretstore "$store" -o jsonpath='{.spec.provider.oracle.vault}')"
region="$(kubectl --context "$context" get clustersecretstore "$store" -o jsonpath='{.spec.provider.oracle.region}')"
compartment="$(oci kms management vault get --vault-id "$vault" --region "$region" \
  --query 'data."compartment-id"' --raw-output)"

if [[ -t 0 ]]; then
  read -r -s -p "Value for ${name}: " value
  echo
else
  value="$(cat)"
fi
if [[ -z $value ]]; then
  echo "empty value, nothing written" >&2
  exit 1
fi
content="$(printf '%s' "$value" | base64 | tr -d '\n')"
unset value

existing="$(oci vault secret list --all --compartment-id "$compartment" --vault-id "$vault" --region "$region" \
  --name "$name" --lifecycle-state ACTIVE --query 'data[0].id' --raw-output 2> /dev/null || true)"

if [[ -n $existing && $existing != "null" ]]; then
  oci vault secret update-base64 --secret-id "$existing" --region "$region" \
    --secret-content-content "$content" --query 'data."secret-name"' --raw-output > /dev/null
  echo "updated ${name}"
else
  # Use the encryption key of any existing secret in the vault.
  key="$(oci vault secret list --all --compartment-id "$compartment" --vault-id "$vault" --region "$region" \
    --lifecycle-state ACTIVE --query 'data[0]."key-id"' --raw-output)"
  oci vault secret create-base64 --compartment-id "$compartment" --vault-id "$vault" --key-id "$key" \
    --region "$region" --secret-name "$name" --secret-content-content "$content" \
    --query 'data."secret-name"' --raw-output > /dev/null
  echo "created ${name}"
fi
