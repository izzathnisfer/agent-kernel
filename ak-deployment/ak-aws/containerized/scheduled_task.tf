# scheduled_task gates all scheduler resources, IAM grants, and routes at once,
# mirroring the queue_mode gate.

module "scheduler" {
  count  = var.scheduled_task ? 1 : 0
  source = "./modules/scheduler"

  product_alias = var.product_alias
  env_alias     = var.env_alias
  module_name   = var.module_name
  prefix        = local.prefix

  scheduled_task_config = var.scheduled_task_config

  # Follows the same variable that picks the session backend, so the two can't disagree.
  create_scheduled_task_table = var.create_dynamodb_memory_table

  input_queue_arn = module.queues[0].input_queue_arn

  tags = var.tags
}
