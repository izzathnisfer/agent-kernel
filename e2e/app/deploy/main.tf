# Containerized module configuration for the Agent Kernel messaging e2e test deployment.
# Deploys a single ECS service running one OpenAI agent with the Slack and Telegram
# integrations enabled, fronted by an HTTPS API Gateway so both platforms can deliver
# webhooks:
#   POST {invoke_url}/api/v1/slack/events      -> /slack/events      (Slack Events API request URL)
#   POST {invoke_url}/api/v1/telegram/webhook  -> /telegram/webhook  (Telegram setWebhook URL)
module "e2e_agents" {
  source  = "yaalalabs/ak-containerized/aws"
  version = "0.8.0"

  # Basic ECS configuration
  product_alias        = var.product_alias
  env_alias            = var.env_alias
  module_name          = var.module_name
  container_type       = "ecs"
  region               = var.region
  vpc_id               = var.vpc_id
  private_subnet_ids   = var.private_subnet_ids
  product_display_name = "AK Messaging Integrations E2E"

  gateway_endpoints = [
    {
      path           = "slack/events",
      method         = "POST",
      overwrite_path = "/slack/events"
    },
    {
      path           = "telegram/webhook",
      method         = "POST",
      overwrite_path = "/telegram/webhook"
    }
  ]

  rest_service = {
    package_path   = "../dist"
    container_port = 8000
    environment_variables = {
      OPENAI_API_KEY              = var.openai_api_key
      SLACK_BOT_TOKEN             = var.slack_bot_token
      SLACK_SIGNING_SECRET        = var.slack_signing_secret
      AK_TELEGRAM__BOT_TOKEN      = var.telegram_bot_token
      AK_TELEGRAM__WEBHOOK_SECRET = var.telegram_webhook_secret
    }
  }
}
