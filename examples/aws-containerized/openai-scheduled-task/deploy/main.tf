# ---------------------------------------------------------------------------
# Containerized Agents Deployment — Scheduled Tasks
# ---------------------------------------------------------------------------
# Scheduling is available on AWS in queue mode only, so this example is a scalable
# queue-mode deployment: a REST service enqueues, an agent runner consumes. When a
# schedule fires, EventBridge Scheduler puts an ordinary agent message on the same input
# queue and the agent runner executes it with no scheduling-specific code path.
module "containerized_agents" {
  # When using from registry:
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
    # Override the Docker CMD to specify the correct entrypoint
    command = ["python", "app_rest_service.py"]
    environment_variables = {
      OPENAI_API_KEY = var.openai_api_key
    }
  }

  # ---- Agent Memory (Session Store) ----
  # Scheduling requires a durable session store. This also decides where scheduled tasks
  # are stored: a DynamoDB session store gives them their own dedicated table, while a
  # Redis/Valkey one reuses that cluster with a separate keyspace and creates no table.
  create_dynamodb_memory_table = true

  # ---- Queue Mode ----
  # Required for scheduling — the timer's target is the input queue.
  queue_mode     = true
  execution_mode = "rest_sync" # must match execution.mode in config.yaml

  # ---- Queue Configuration ----
  # SQS queues for request/response handling. Both are FIFO (hardcoded by the module),
  # which scheduling depends on: fires are grouped by scheduled_task_id so a task's runs
  # are serialized, and deduplicated by scheduled_task_id + scheduled_time so a duplicate
  # timer delivery cannot run twice.
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
    # Provide package_path to build a separate Docker image for agent runner
    package_path = "../dist-agent-runner"
    # Override the Docker CMD to specify the correct entrypoint
    command = ["python", "app_agent_runner.py"]
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
  # One gate for the whole capability: the scheduled-task table, the EventBridge Scheduler
  # schedule group, the timer's execution role, and the component IAM grants. Requires
  # queue_mode = true, which Terraform validates before the app can fail at startup.
  scheduled_task = true

  scheduled_task_config = {
    # Let the agent create and manage its own scheduled tasks. Off by default: this is what
    # gives the agent runner scheduler permissions at all. Leave it false and the runner
    # gets no table or scheduler access, while the REST routes keep working.
    enable_agent_tools = true
  }

  tags = {
    Example     = "openai-scheduled-task"
    Environment = var.env_alias
  }
}
