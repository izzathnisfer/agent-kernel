# Agent-initiated Slack conversations using the scalable serverless module
module "serverless_agents" {
  source  = "yaalalabs/ak-serverless/aws"
  version = "0.6.1"

  # Basic configuration
  product_alias        = var.product_alias
  env_alias             = var.env_alias
  module_name           = var.module_name
  product_display_name  = "AK Slack Agent-Initiated Conversations Example"
  region                = var.region
  is_production         = var.is_production
  vpc_id                = var.vpc_id
  private_subnet_ids    = var.private_subnet_ids

  # Execution mode - queue mode required for the request/agent-runner/response split.
  # The mode only affects the built-in /api/v1/chat endpoint (unused by the Slack
  # flow, which goes through the /slack/events custom route below).
  queue_mode     = true
  execution_mode = "rest_sync" # rest_sync or rest_async

  # Memory DB Config - uses existing Redis cluster
  create_redis_cluster = false

  # Response Store Config - uses existing Redis cluster
  create_redis_response_store = false

  # API Gateway configuration
  api_version    = "v1"
  api_base_path  = "api"
  agent_endpoint = "chat"

  # Custom API endpoint for Slack's Events API webhook
  gateway_endpoints = [
    {
      path   = "slack/events"
      method = "POST"
    }
  ]

  # Request handler configuration
  request_handler = {
    module_name           = "rqst-hdlr"
    function_name         = "rqh-func"
    function_description  = "Slack webhook receiver — resolves session id, enqueues"
    handler_path          = "lambda_request_handler.handler"
    package_type          = "Image"
    package_path          = "../dist_request_handler"
    memory_size           = 256
    timeout               = 45 # cold start opens session/response-store connections before any route runs
    environment_variables = {
      "OPENAI_API_KEY"        = var.openai_api_key
      "SLACK_BOT_TOKEN"       = var.slack_bot_token
      "SLACK_SIGNING_SECRET"  = var.slack_signing_secret
    }
  }

  # Agent runner configuration
  agent_runner = {
    module_name           = "agent-runner"
    function_name         = "ar-func"
    function_description  = "Runs the two-agent Slack notification flow"
    timeout               = 45
    memory_size           = 512
    handler_path           = "lambda_agent_runner.handler"
    package_type           = "Image"
    package_path           = "../dist_agent_runner"
    environment_variables = {
      "OPENAI_API_KEY" = var.openai_api_key
    }
  }

  # Response handler configuration
  response_handler = {
    function_name         = "rsh-func"
    module_name           = "rspns-hdlr"
    function_description  = "Delivers agent replies (and initiated conversations) to Slack"
    timeout               = 45
    memory_size           = 256
    handler_path           = "lambda_response_handler.handler"
    package_type           = "Image"
    package_path           = "../dist_response_handler"
    environment_variables = {
      "SLACK_BOT_TOKEN" = var.slack_bot_token
    }
  }

  # Queue configuration for scalable processing
  queue_config = {
    input_queue_visibility_timeout        = 60
    input_queue_max_receive_count         = 3
    input_queue_create_dlq                = false
    input_queue_message_retention_seconds = 300

    output_queue_visibility_timeout        = 60
    output_queue_max_receive_count         = 3
    output_queue_create_dlq                = false
    output_queue_message_retention_seconds = 300

    batch_size                         = 10
    maximum_batching_window_in_seconds = 0
  }
}
