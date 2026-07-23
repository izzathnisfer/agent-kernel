locals {
  package_type = var.is_layer ? "layer" : "lambda"
  file_exist   = fileexists(var.package_dir_path)
  package_hash      = local.file_exist ? filemd5(var.package_dir_path) : null
  package_file_name = local.file_exist ? "source_code-${local.package_hash}.zip" : "source_code.zip"

  key = "${var.product_alias}/${var.region}/${var.env_alias}/${var.module_name}/${local.package_type}/${local.package_file_name}"
}

data "aws_s3_object" "source_code_object" {
  count  = local.file_exist ? 0 : 1
  bucket = var.s3_bucket
  key    = local.key
}
resource "aws_s3_object" "source_code" {
  bucket        = var.s3_bucket
  key           = local.key
  source        = local.file_exist ? var.package_dir_path : local.package_file_name
  etag          = local.file_exist ? local.package_hash : data.aws_s3_object.source_code_object[0].etag
  force_destroy = false
}