# Provisions the two S3 buckets the OKF S3 run path needs, plus the two IAM
# managed policies that encode the read-write / read-only split:
#
#   * bundle (OKF wiki) bucket — read-write  (GetObject / PutObject / ListBucket)
#   * source bucket            — read-only   (GetObject / ListBucket only)
#
# The two policies are the AWS-boundary form of the in-process permission model
# (which tools each agent is bound). This module provisions buckets and policies
# ONLY — it creates no IAM users and wires no credentials. The operator attaches
# the emitted policies to whatever principal runs the demo (see the README and
# the design's Non-goals). The application code never creates buckets: S3Storage
# assumes they already exist.

terraform {
  required_version = ">= 1.3.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}

locals {
  # Normalize the prefixes to a trailing-slash form for object ARNs; empty means
  # the whole bucket.
  bundle_prefix = var.bundle_prefix == "" ? "" : "${trimsuffix(var.bundle_prefix, "/")}/"
  source_prefix = var.source_prefix == "" ? "" : "${trimsuffix(var.source_prefix, "/")}/"

  bundle_objects = "${aws_s3_bucket.bundle.arn}/${local.bundle_prefix}*"
  source_objects = "${aws_s3_bucket.source.arn}/${local.source_prefix}*"
}

# --------------------------------------------------------------------------- #
# Buckets
# --------------------------------------------------------------------------- #

resource "aws_s3_bucket" "bundle" {
  bucket = var.bundle_bucket_name
  tags   = var.tags
}

resource "aws_s3_bucket" "source" {
  bucket = var.source_bucket_name
  tags   = var.tags
}

resource "aws_s3_bucket_public_access_block" "bundle" {
  bucket                  = aws_s3_bucket.bundle.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_public_access_block" "source" {
  bucket                  = aws_s3_bucket.source.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# --------------------------------------------------------------------------- #
# IAM policies — read-write bundle, read-only source
# --------------------------------------------------------------------------- #

# Read-write on the bundle bucket: the demo reads, writes, and lists the bundle.
data "aws_iam_policy_document" "bundle_rw" {
  statement {
    sid       = "BundleObjectsReadWrite"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = [local.bundle_objects]
  }

  statement {
    sid       = "BundleListBucket"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.bundle.arn]

    dynamic "condition" {
      for_each = local.bundle_prefix == "" ? [] : [local.bundle_prefix]
      content {
        test     = "StringLike"
        variable = "s3:prefix"
        values   = ["${condition.value}*"]
      }
    }
  }
}

# Read-only on the source bucket: the Curator only ever reads and lists the
# source — never PutObject / DeleteObject. This is the defence-in-depth grant
# beyond the tool layer.
data "aws_iam_policy_document" "source_ro" {
  statement {
    sid       = "SourceObjectsReadOnly"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = [local.source_objects]
  }

  statement {
    sid       = "SourceListBucket"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.source.arn]

    dynamic "condition" {
      for_each = local.source_prefix == "" ? [] : [local.source_prefix]
      content {
        test     = "StringLike"
        variable = "s3:prefix"
        values   = ["${condition.value}*"]
      }
    }
  }
}

resource "aws_iam_policy" "bundle_rw" {
  name        = "${var.bundle_bucket_name}-okf-bundle-rw"
  description = "Read-write access to the OKF bundle bucket for the OKF demo."
  policy      = data.aws_iam_policy_document.bundle_rw.json
  tags        = var.tags
}

resource "aws_iam_policy" "source_ro" {
  name        = "${var.source_bucket_name}-okf-source-ro"
  description = "Read-only access to the OKF source bucket for the OKF demo Curator."
  policy      = data.aws_iam_policy_document.source_ro.json
  tags        = var.tags
}
