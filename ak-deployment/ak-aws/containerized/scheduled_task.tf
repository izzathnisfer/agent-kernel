# Scheduled Tasks
# One gate: scheduled_task = true creates every scheduler resource, IAM permission and
# route; false (the default) creates none of them and leaves the deployment
# byte-identical to today. Mirrors the queue_mode gate.

module "scheduler" {
  count  = var.scheduled_task ? 1 : 0
  source = "./modules/scheduler"

  product_alias = var.product_alias
  env_alias     = var.env_alias
  module_name   = var.module_name
  prefix        = local.prefix

  scheduled_task_config = var.scheduled_task_config

  # The scheduled-task store follows the session store type, derived from the same
  # variable the deployment already uses to pick its session backend so the two can
  # never disagree.
  create_scheduled_task_table = var.create_dynamodb_memory_table

  input_queue_arn = module.queues[0].input_queue_arn

  tags = var.tags
}
