#!/bin/bash
#
# Provision the S3 run path for the OKF example: two buckets (read-write bundle,
# read-only source) and their IAM policies. This module only provisions storage
# and policies — it creates no IAM users and no compute. Attach the emitted
# bundle_rw_policy_arn / source_ro_policy_arn to whatever principal runs the demo
# (see the README).
#
# Usage:
#   ./deploy.sh            # terraform init + apply
#   ./deploy.sh destroy    # terraform destroy
#
# Using local state? Delete the optional remote backend first: rm backend.tf

set -euo pipefail

cd "$(dirname "$0")"

if ! command -v terraform >/dev/null 2>&1; then
  echo "error: terraform is not installed or not on PATH" >&2
  exit 1
fi

# Bootstrap terraform.tfvars from the example on first run so apply never runs
# against placeholder bucket names.
if [[ ! -f terraform.tfvars ]]; then
  cp terraform.tfvars.example terraform.tfvars
  echo "Created terraform.tfvars from the example."
  echo "Edit it (bucket names must be globally unique), then re-run ./deploy.sh."
  exit 0
fi

terraform init

if [[ ${1-} == "destroy" ]]; then
  terraform destroy
else
  terraform apply
fi
