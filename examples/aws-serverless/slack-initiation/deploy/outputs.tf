output "agent_invoke_url" {
  description = "The base URL for this deployment — append /slack/events for the Slack Request URL"
  value       = module.serverless_agents.agent_invoke_url
}
