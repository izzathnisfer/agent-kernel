#!/bin/bash
#
# Provision the S3 run path for the OKF example: two buckets (read-write bundle,
# read-only source) and their IAM policies. This module only provisions storage
# and policies — it creates no IAM users and no compute. Attach the emitted
# bundle_rw_policy_arn / source_ro_policy_arn to whatever principal runs the demo
# (see the README).
#
# Usage:
#   ./deploy.sh                       # terraform init + apply (local state)
#   ./deploy.sh destroy               # terraform destroy
#   OKF_REMOTE_STATE=1 ./deploy.sh    # opt into an S3 remote-state backend
#
# State is local by default. Set OKF_REMOTE_STATE=1 to opt into remote state:
# on first run this copies backend.tf.example -> backend.tf (git-ignored) for you
# to edit (point it at a bucket you own), then re-run ./deploy.sh.

set -euo pipefail

cd "$(dirname "$0")"

if ! command -v terraform >/dev/null 2>&1; then
  echo "error: terraform is not installed or not on PATH" >&2
  exit 1
fi

# Opt into remote state only when asked. On first run copy the backend template
# into place (git-ignored) so terraform init never auto-loads a placeholder
# bucket; the user edits the copy before re-running.
if [[ ${OKF_REMOTE_STATE-} == "1" && ! -f backend.tf ]]; then
  cp backend.tf.example backend.tf
  echo "Created backend.tf from backend.tf.example (remote state)."
  echo "Edit it (set your Terraform state bucket), then re-run ./deploy.sh."
  exit 0
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
