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

output "schedule_arn_pattern" {
  description = "ARN pattern matching every schedule in this deployment's group; scopes the component IAM grants"
  value       = local.schedule_arn_pattern
}

output "target_role_arn" {
  description = "IAM role EventBridge Scheduler assumes to send a fire to the input queue"
  value       = aws_iam_role.scheduler_target.arn
}
