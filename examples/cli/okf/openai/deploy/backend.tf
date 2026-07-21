# OPTIONAL: remote Terraform state backend.
# Delete this file to use local state instead (no remote state is required for
# this example — see the design's Non-goals). If you keep it, point it at a
# bucket you control and re-run `terraform init`.

terraform {
  backend "s3" {
    bucket       = "CHANGE-ME-terraform-state-bucket"
    key          = "agent-kernel/examples/okf/terraform.tfstate"
    region       = "us-east-1"
    use_lockfile = true
    encrypt      = true
  }
}
