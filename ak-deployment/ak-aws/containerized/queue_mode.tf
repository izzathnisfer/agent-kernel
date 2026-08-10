# Queue Mode Resources
# This file orchestrates SQS queues, agent runner, and related resources for queue-based execution

# SQS Queues Module

module "queues" {
  count  = var.queue_mode ? 1 : 0
  source = "./modules/queues"

  product_alias = var.product_alias
  env_alias     = var.env_alias
  module_name   = var.module_name

  queue_config = var.queue_config

  tags = var.tags
}

# Agent Runner Module

module "agent_runner" {
  count  = var.queue_mode ? 1 : 0
  source = "./modules/agent-runner"

  product_alias = var.product_alias
  env_alias     = var.env_alias
  module_name   = var.module_name
  region        = var.region
  prefix        = local.prefix

  vpc_id     = local.vpc_id
  subnet_ids = local.subnet_ids

  ecs_cluster_arn  = module.ecs.cluster_arn
  ecs_cluster_name = module.ecs.cluster_name

  input_queue_url  = module.queues[0].input_queue_url
  input_queue_arn  = module.queues[0].input_queue_arn
  output_queue_url = module.queues[0].output_queue_url
  output_queue_arn = module.queues[0].output_queue_arn

  redis_url                    = local.redis_url
  valkey_url                   = local.valkey_url
  create_dynamodb_memory_table = var.create_dynamodb_memory_table
  dynamodb_memory_table_arn    = local.dynamodb_memory_table_arn
  dynamodb_memory_table_name   = local.dynamodb_memory_table_name
  create_dynamodb_thread_table = var.create_dynamodb_thread_table
  dynamodb_thread_table_arn    = local.dynamodb_thread_table_arn
  dynamodb_thread_table_name   = local.dynamodb_thread_table_name

  agent_runner = {
    cpu           = var.agent_runner.cpu
    memory        = var.agent_runner.memory
    desired_count = var.agent_runner.desired_count
    # Use agent_runner image if package_path provided, else use image_uri, else fallback to rest_service image
    image_uri             = var.agent_runner.package_path != null ? module.agent_runner_docker_image[0].docker_image_uri : (var.agent_runner.image_uri != null ? var.agent_runner.image_uri : module.docker_image[0].docker_image_uri)
    command               = var.agent_runner.command
    environment_variables = merge(var.environment_variables, var.agent_runner.environment_variables)
  }
  queue_config   = var.queue_config
  scaling_config = var.scaling_config
  execution_mode = var.execution_mode

  default_image_uri = module.docker_image[0].docker_image_uri

  account_id = data.aws_caller_identity.current.account_id

  # Scheduled tasks: the runner only needs access when the agent-callable tools are on.
  scheduled_task                     = var.scheduled_task
  scheduled_task_config              = var.scheduled_task_config
  scheduled_task_table_name          = var.scheduled_task ? module.scheduler[0].table_name : null
  scheduled_task_table_arn           = var.scheduled_task ? module.scheduler[0].table_arn : null
  scheduled_task_schedule_group_name = var.scheduled_task ? module.scheduler[0].schedule_group_name : null
  scheduled_task_schedule_group_arn  = var.scheduled_task ? module.scheduler[0].schedule_group_arn : null
  scheduled_task_target_role_arn     = var.scheduled_task ? module.scheduler[0].target_role_arn : null

  tags = var.tags
}
