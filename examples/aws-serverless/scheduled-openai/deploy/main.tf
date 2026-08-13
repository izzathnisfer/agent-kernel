# ---------------------------------------------------------------------------
# Serverless Agents Deployment — Scheduled Tasks
# ---------------------------------------------------------------------------
# Queue-mode deployment: request handler enqueues, agent runner consumes, response handler
# records outcomes. Scheduled fires are just queue messages EventBridge Scheduler injects.
module "serverless_agents" {
  # NOTE: scheduled_task(_config) need a module version newer than 0.8.1. Until that's
  # published, point source at the in-repo module instead and drop version:
  #   source = "../../../../ak-deployment/ak-aws/serverless"
  source  = "yaalalabs/ak-serverless/aws"
  version = "0.8.1"

  providers            = { aws = aws, docker = docker }
  product_alias        = var.product_alias
  env_alias            = var.env_alias
  module_name          = var.module_name
  product_display_name = "AK OpenAI Scheduled Tasks Serverless Example"
  region               = var.region
  is_production        = var.is_production

  # Required for scheduling: the timer's target is the input queue.
  queue_mode     = true
  execution_mode = "rest_sync" # rest_sync or rest_async

  # ---- Agent Memory (Session Store) ----
  # Scheduling needs a durable session store. DynamoDB gives scheduled tasks their own
  # table; Redis/Valkey would reuse the cluster's keyspace instead.
  create_dynamodb_memory_table = true

  create_dynamodb_response_store = true

  api_version    = "v1"
  api_base_path  = "api"
  agent_endpoint = "chat"

  # ---- Identity ----
  # Scheduling requires an authenticated owner per task; on serverless that identity comes
  # from this authorizer's principalId. Omitting it still deploys, but every /schedule
  # request then fails with 401 since there's no authorizer context.
  authorizer = {
    description           = "Resolves the bearer token to the user id that owns a scheduled task"
    function_name         = "gtwy-auth"
    handler_path          = "lambda_auth.handler"
    package_path          = "../dist_auth.zip"
    package_type          = "LocalZip"
    module_name           = "sched-auth"
    result_ttl_in_seconds = 0
  }

  # Hosts the chat create path and the /api/v1/schedule management routes.
  request_handler = {
    module_name          = "rqst-hdlr"
    function_name        = "rqh-func"
    function_description = "Agent Kernel OpenAI Scheduled Tasks Sample Lambda"
    handler_path         = "lambda_request_handler.handler"
    package_type         = "LocalZip"
    package_path         = "../dist_request_handler.zip"
    memory_size          = 256
    timeout              = 45
    environment_variables = {
      "OPENAI_API_KEY" = var.openai_api_key
    }
  }

  # Executes both ordinary requests and scheduled fires; it cannot tell them apart and
  # doesn't need to.
  agent_runner = {
    module_name          = "agent-runner"
    function_name        = "ar-func"
    function_description = "Agent runner for processing OpenAI requests and scheduled fires"
    timeout              = 45
    memory_size          = 512
    handler_path         = "lambda_agent_runner.handler"
    package_type         = "Image"
    package_path         = "../dist_agent_runner"
    environment_variables = {
      "OPENAI_API_KEY" = var.openai_api_key
    }
  }

  # Also records a scheduled run's outcome back onto its row.
  response_handler = {
    function_name        = "rsh-func"
    module_name          = "rspns-hdlr"
    function_description = "Response handler for processing completed requests"
    timeout              = 45
    memory_size          = 256
    handler_path         = "lambda_response_handler.handler"
    package_type         = "LocalZip"
    package_path         = "../dist_response_handler.zip"
  }

  queue_config = {
    # FIFO is required for scheduling: it groups fires by scheduled_task_id to serialize a
    # task's runs, and dedupes by scheduled_task_id + scheduled_time. Already the default;
    # set explicitly to keep the requirement visible.
    fifo_queue = true

    # Input queue settings
    input_queue_visibility_timeout        = 60 # must exceed the lambda timeout to avoid double-processing
    input_queue_max_receive_count         = 3
    input_queue_create_dlq                = false
    input_queue_message_retention_seconds = 300

    # Output queue settings
    output_queue_visibility_timeout        = 60 # must exceed the lambda timeout to avoid double-processing
    output_queue_max_receive_count         = 3
    output_queue_create_dlq                = false
    output_queue_message_retention_seconds = 300

    # Processing settings
    batch_size                         = 10
    maximum_batching_window_in_seconds = 0
  }

  # ---- Scheduled Tasks ----
  # Gates the scheduled-task table, EventBridge Scheduler group, timer role, IAM grants, and
  # /api/v1/schedule routes. Requires queue_mode = true.
  scheduled_task = true

  scheduled_task_config = {
    # Lets the agent create/manage its own scheduled tasks via tools; off by default. False
    # means no scheduler access for the runner, though REST routes still work.
    enable_agent_tools = true
  }
}
