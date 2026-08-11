# ---------------------------------------------------------------------------
# Serverless Agents Deployment — Scheduled Tasks
# ---------------------------------------------------------------------------
# Scheduling is available on AWS in queue mode only, so this example is a scalable
# queue-mode deployment: a request handler enqueues, an agent runner consumes, a response
# handler records outcomes. When a schedule fires, EventBridge Scheduler puts an ordinary
# agent message on the same input queue and the agent runner executes it with no
# scheduling-specific code path.
module "serverless_agents" {
  source  = "yaalalabs/ak-serverless/aws"
  version = "0.8.1"

  providers = { aws = aws, docker = docker }
  # Basic configuration
  product_alias        = var.product_alias
  env_alias            = var.env_alias
  module_name          = var.module_name
  product_display_name = "AK OpenAI Scheduled Tasks Serverless Example"
  region               = var.region
  is_production        = var.is_production

  # Execution mode - required for scheduling, since the timer's target is the input queue
  queue_mode     = true
  execution_mode = "rest_sync" # rest_sync or rest_async

  # ---- Agent Memory (Session Store) ----
  # Scheduling requires a durable session store. This also decides where scheduled tasks
  # are stored: a DynamoDB session store gives them their own dedicated table, while a
  # Redis/Valkey one reuses that cluster with a separate keyspace and creates no table.
  # DynamoDB keeps this example self-contained — no VPC or cache cluster needed.
  create_dynamodb_memory_table = true

  # Response Store Config
  create_dynamodb_response_store = true

  # API Gateway configuration
  api_version    = "v1"
  api_base_path  = "api"
  agent_endpoint = "chat"

  # ---- Identity ----
  # The authorizer is what makes scheduling usable here. Every scheduled task must have an
  # authenticated owner, and on serverless that identity comes from API Gateway: the
  # authorizer's principalId (ValidationResult.subject in lambda_auth.py) becomes the
  # owner. API Gateway attaches this authorizer to every route, including the /schedule
  # routes the module adds — there is no per-route opt-out.
  #
  # Omit this block and the deployment still applies, but every schedule request is
  # rejected with 401 because the event carries no authorizer context.
  authorizer = {
    description           = "Resolves the bearer token to the user id that owns a scheduled task"
    function_name         = "gtwy-auth"
    handler_path          = "lambda_auth.handler"
    package_path          = "../dist_auth.zip"
    package_type          = "LocalZip"
    module_name           = "sched-auth"
    result_ttl_in_seconds = 0
  }

  # Request handler configuration
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

  # Agent runner configuration
  # Executes both ordinary requests and scheduled fires. It cannot tell them apart, and
  # does not need to.
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

  # Response handler configuration
  # Also the component that records a scheduled run's outcome back onto its row.
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

  # Queue configuration for scalable processing
  queue_config = {
    # FIFO is a precondition for scheduling, not a preference: fires are grouped by
    # scheduled_task_id so a task's runs are serialized, and deduplicated by
    # scheduled_task_id + scheduled_time so a duplicate timer delivery cannot run twice.
    # It already defaults to true; set explicitly so the requirement is visible.
    fifo_queue = true

    # Input queue settings
    input_queue_visibility_timeout        = 60 # make sure to set it higher than the lambda timeout to avoid multiple processing of the same message
    input_queue_max_receive_count         = 3
    input_queue_create_dlq                = false
    input_queue_message_retention_seconds = 300

    # Output queue settings
    output_queue_visibility_timeout        = 60 # make sure to set it higher than the lambda timeout to avoid multiple processing of the same message
    output_queue_max_receive_count         = 3
    output_queue_create_dlq                = false
    output_queue_message_retention_seconds = 300

    # Processing settings
    batch_size                         = 10
    maximum_batching_window_in_seconds = 0
  }

  # ---- Scheduled Tasks ----
  # One gate for the whole capability: the scheduled-task table, the EventBridge Scheduler
  # schedule group, the timer's execution role, the component IAM grants, and the
  # /api/v1/schedule API Gateway routes. Requires queue_mode = true, which Terraform
  # validates before the app can fail at startup.
  scheduled_task = true

  scheduled_task_config = {
    # Let the agent create and manage its own scheduled tasks. Off by default: this is what
    # gives the agent runner scheduler permissions at all. Leave it false and the runner
    # gets no table or scheduler access, while the REST routes keep working.
    enable_agent_tools = true
  }
}
