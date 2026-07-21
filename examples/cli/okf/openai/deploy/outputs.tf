# These feed straight into the demo's S3Storage constructor params:
#   OKF_BACKEND=s3
#   OKF_BUNDLE_BUCKET=<bundle_bucket_name>  OKF_BUNDLE_PREFIX=<bundle_prefix>
#   OKF_SOURCE_BUCKET=<source_bucket_name>  OKF_SOURCE_PREFIX=<source_prefix>
#   AWS_REGION=<region>
# Attach bundle_rw_policy_arn and source_ro_policy_arn to the principal that
# runs the demo (see the README).

output "bundle_bucket_name" {
  description = "Name of the OKF bundle (read-write) bucket."
  value       = aws_s3_bucket.bundle.bucket
}

output "bundle_prefix" {
  description = "Bundle root prefix inside the bundle bucket."
  value       = var.bundle_prefix
}

output "source_bucket_name" {
  description = "Name of the source (read-only) bucket."
  value       = aws_s3_bucket.source.bucket
}

output "source_prefix" {
  description = "Source folder prefix inside the source bucket."
  value       = var.source_prefix
}

output "region" {
  description = "AWS region the buckets were created in."
  value       = var.region
}

output "bundle_rw_policy_arn" {
  description = "ARN of the read-write policy for the bundle bucket — attach to the demo principal."
  value       = aws_iam_policy.bundle_rw.arn
}

output "source_ro_policy_arn" {
  description = "ARN of the read-only policy for the source bucket — attach to the demo principal."
  value       = aws_iam_policy.source_ro.arn
}
