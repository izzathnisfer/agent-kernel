output "agent_invoke_url" {
  description = "Chat endpoint URL — replace trailing /chat with /slack/events for the Slack Request URL"
  value       = module.containerized_agents.agent_invoke_url
}

output "input_queue_url" {
  description = "SQS Input Queue URL"
  value       = module.containerized_agents.input_queue_url
}

output "output_queue_url" {
  description = "SQS Output Queue URL"
  value       = module.containerized_agents.output_queue_url
}

output "agent_runner_service_name" {
  description = "ECS Agent Runner service name"
  value       = module.containerized_agents.agent_runner_service_name
}

output "session_id_mapping_table_name" {
  description = "DynamoDB Session ID Mapping table name (agent-initiated conversations)"
  value       = module.containerized_agents.session_id_mapping_table_name
}
