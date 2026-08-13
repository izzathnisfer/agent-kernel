# ---------------------------------------------------------------------------
# Containerized Agents Deployment — Scheduled Tasks
# ---------------------------------------------------------------------------
# Queue-mode deployment: REST service enqueues, agent runner consumes. Scheduled fires are
# just queue messages EventBridge Scheduler injects.
module "containerized_agents" {
  # NOTE: scheduled_task(_config) need a module version newer than 0.8.1. Until that's
  # published, point source at the in-repo module instead and drop version:
  #   source = "../../../../ak-deployment/ak-aws/containerized"
  source  = "yaalalabs/ak-containerized/aws"
  version = "0.8.1"

  providers            = { aws = aws, docker = docker }
  product_alias        = var.product_alias
  env_alias            = var.env_alias
  module_name          = var.module_name
  region               = var.region
  product_display_name = "OpenAI Agents - Scheduled Tasks"

  vpc_id             = var.vpc_id
  private_subnet_ids = var.private_subnet_ids


  # ---- REST Service Configuration ----
  # Hosts the chat create path and the /api/v1/schedule management routes. Both require an
  # Authoriser, supplied in app_rest_service.py.
  rest_service = {
    package_path          = "../dist-rest-service"
    cpu                   = 256
    memory                = 512
    desired_count         = 1
    container_port        = 8000
    health_check_endpoint = "/health"
    command               = ["python", "app_rest_service.py"]
    environment_variables = {
      OPENAI_API_KEY = var.openai_api_key
    }
  }

  # ---- Agent Memory (Session Store) ----
  # Scheduling needs a durable session store. DynamoDB gives scheduled tasks their own
  # table; Redis/Valkey would reuse the cluster's keyspace instead.
  create_dynamodb_memory_table = true

  # ---- Queue Mode ----
  # Required for scheduling — the timer's target is the input queue.
  queue_mode     = true
  execution_mode = "rest_sync" # must match execution.mode in config.yaml

  # ---- Queue Configuration ----
  # Queues are FIFO (hardcoded by the module): scheduling needs fires grouped by
  # scheduled_task_id to serialize a task's runs, deduped by scheduled_task_id + time.
  queue_config = {
    # Optional: customize queue names
    input_queue_name  = "input-queue"  # Default
    output_queue_name = "output-queue" # Default

    # Input queue settings (requests from REST service to agent runner)
    input_queue_visibility_timeout        = 120 # Should be >= agent processing time
    input_queue_message_retention_seconds = 1800
    input_queue_max_receive_count         = 3
    input_queue_create_dlq                = true

    # Output queue settings (responses from agent runner to REST service)
    output_queue_visibility_timeout        = 60
    output_queue_message_retention_seconds = 1800
    output_queue_max_receive_count         = 3
    output_queue_create_dlq                = true

    # Shared settings
    sqs_managed_sse_enabled   = true
    max_message_size          = 262144 # 256 KB
    receive_wait_time_seconds = 0      # Long polling disabled
  }

  # ---- Agent Runner Configuration ----
  # Executes both ordinary requests and scheduled fires. It cannot tell them apart, and
  # does not need to.
  agent_runner = {
    cpu           = 1024
    memory        = 2048
    desired_count = 1
    package_path  = "../dist-agent-runner"
    command       = ["python", "app_agent_runner.py"]
    environment_variables = {
      OPENAI_API_KEY = var.openai_api_key
    }
  }

  # ---- Agent Runner Auto Scaling ----
  # Scale based on queue depth (BacklogPerTask metric)
  scaling_config = {
    enabled = true

    # Scaling limits
    min_count = 1  # Minimum tasks (can be 0 to scale to zero)
    max_count = 10 # Maximum tasks

    # Scaling behavior
    backlog_target = 10 # Target messages per task (scale up when exceeded)

    # Cooldown periods (prevent flapping)
    scale_in_cooldown  = 120 # Wait 2min before scaling in again
    scale_out_cooldown = 30  # Wait 30s before scaling out again
  }

  # ---- Scheduled Tasks ----
  # Gates the scheduled-task table, EventBridge Scheduler group, timer role, and IAM grants.
  # Requires queue_mode = true.
  scheduled_task = true

  scheduled_task_config = {
    # Lets the agent create/manage its own scheduled tasks via tools; off by default. False
    # means no scheduler access for the runner, though REST routes still work.
    enable_agent_tools = true
  }

  tags = {
    Example     = "openai-scheduled-task"
    Environment = var.env_alias
  }
}
