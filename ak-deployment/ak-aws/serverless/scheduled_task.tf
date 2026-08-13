# Scheduled Tasks
# One gate: scheduled_task = true creates every scheduler resource/IAM permission/route;
# false (default) creates none, matching the queue_mode pattern.

module "scheduler" {
  count  = var.scheduled_task ? 1 : 0
  source = "./modules/scheduler"

  product_alias = var.product_alias
  env_alias     = var.env_alias
  module_name   = var.module_name
  prefix        = "${var.product_alias}-${var.env_alias}-${var.module_name}"

  scheduled_task_config = var.scheduled_task_config

  # Follows the session store type via the same variable, so the two can never disagree.
  create_scheduled_task_table = var.create_dynamodb_memory_table

  input_queue_arn = local.input_queue_arn

  tags = var.tags
}
