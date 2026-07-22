# ---------------------------------------------------------------------------
# Agent-Initiated Slack Conversations — Containerized Deployment
# ---------------------------------------------------------------------------

module "containerized_agents" {
  source  = "yaalalabs/ak-containerized/aws"
  version = "0.6.1"

  product_alias        = var.product_alias
  env_alias            = var.env_alias
  module_name          = var.module_name
  region               = var.region
  product_display_name = "Slack Agent-Initiated Conversations Example"

  vpc_id             = var.vpc_id
  private_subnet_ids = var.private_subnet_ids

  # Custom API endpoint for Slack's Events API webhook (mirrors serverless slack-initiation).
  # External path is /api/v1/slack/events; overwrite_path rewrites to FastAPI's /slack/events.
  gateway_endpoints = [
    {
      path           = "slack/events"
      method         = "POST"
      overwrite_path = "/slack/events"
    }
  ]

  # ---- REST Service Configuration ----
  rest_service = {
    package_path          = "../dist-rest-service"
    cpu                   = 256
    memory                = 512
    desired_count         = 1
    container_port        = 8000
    health_check_endpoint = "/health"
    command               = ["python", "app_rest_service.py"]
    environment_variables = {
      OPENAI_API_KEY       = var.openai_api_key
      SLACK_BOT_TOKEN      = var.slack_bot_token
      SLACK_SIGNING_SECRET = var.slack_signing_secret
    }
  }

  # ---- Agent Memory (Session Store) ----
  # Uses the existing shared Redis cluster (see config.yaml) — no create_redis_cluster
  # / create_dynamodb_memory_table flag needed since we're not provisioning a new store.

  # ---- Agent-Initiated Conversations ----
  # The Session ID Mapping store follows session.type (Redis here) and rides the same
  # cluster, so no extra AWS resource is needed — conversation_initiation only provisions
  # a DynamoDB table, which is for a DynamoDB-backed session store.
  conversation_initiation = false

  # ---- Queue Mode ----
  queue_mode     = true
  execution_mode = "sync" # the built-in /api/v1/chat endpoint is unused by the Slack flow either way

  # ---- Queue Configuration ----
  queue_config = {
    input_queue_name  = "input-queue"
    output_queue_name = "output-queue"

    input_queue_visibility_timeout        = 120
    input_queue_message_retention_seconds = 1800
    input_queue_max_receive_count         = 3
    input_queue_create_dlq                = true

    output_queue_visibility_timeout        = 60
    output_queue_message_retention_seconds = 1800
    output_queue_max_receive_count         = 3
    output_queue_create_dlq                = true

    sqs_managed_sse_enabled   = true
    max_message_size          = 262144
    receive_wait_time_seconds = 0
  }

  # ---- Agent Runner Configuration ----
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
  scaling_config = {
    enabled = true

    min_count = 1
    max_count = 10

    backlog_target = 10

    scale_in_cooldown  = 120
    scale_out_cooldown = 30
  }

  tags = {
    Example     = "slack-initiation"
    Environment = var.env_alias
  }
}
