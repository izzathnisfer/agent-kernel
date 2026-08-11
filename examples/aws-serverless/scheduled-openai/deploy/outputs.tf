output "agent_invoke_url" {
  description = "POST to this URL to chat with the agent, or to create a scheduled task"
  value       = module.serverless_agents.agent_invoke_url
}

output "scheduled_task_table_name" {
  description = "DynamoDB table holding scheduled task definitions and last-run state"
  value       = module.serverless_agents.scheduled_task_table_name
}

output "scheduled_task_schedule_group_name" {
  description = "EventBridge Scheduler schedule group holding this deployment's timers"
  value       = module.serverless_agents.scheduled_task_schedule_group_name
}
