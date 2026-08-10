output "table_name" {
  description = "Scheduled-task DynamoDB table name; null when the session store is Redis/Valkey"
  value       = var.create_scheduled_task_table ? aws_dynamodb_table.scheduled_tasks[0].name : null
}

output "table_arn" {
  description = "Scheduled-task DynamoDB table ARN; null when the session store is Redis/Valkey"
  value       = var.create_scheduled_task_table ? aws_dynamodb_table.scheduled_tasks[0].arn : null
}

output "schedule_group_name" {
  description = "EventBridge Scheduler schedule group for this deployment"
  value       = aws_scheduler_schedule_group.this.name
}

output "schedule_group_arn" {
  description = "ARN of the EventBridge Scheduler schedule group; scopes the component IAM grants"
  value       = aws_scheduler_schedule_group.this.arn
}

output "target_role_arn" {
  description = "IAM role EventBridge Scheduler assumes to send a fire to the input queue"
  value       = aws_iam_role.scheduler_target.arn
}
