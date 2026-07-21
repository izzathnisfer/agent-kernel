variable "region" {
  description = "AWS region to create the S3 buckets in."
  type        = string
}

variable "bundle_bucket_name" {
  description = "Name of the OKF bundle (wiki) bucket — the durable home of the bundle. The demo needs read-write access to it."
  type        = string
}

variable "bundle_prefix" {
  description = "Key prefix that acts as the bundle root inside the bundle bucket (empty = bucket root)."
  type        = string
  default     = ""
}

variable "source_bucket_name" {
  description = "Name of the source bucket the Curator syncs from. The demo needs read-only access to it."
  type        = string
}

variable "source_prefix" {
  description = "Key prefix of the source folder inside the source bucket (empty = bucket root)."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Tags applied to the created buckets."
  type        = map(string)
  default = {
    Project = "agent-kernel-okf-example"
  }
}
