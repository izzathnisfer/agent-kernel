variable "product_alias" {
  type        = string
  description = "Product alias used in resource names"
}

variable "env_alias" {
  type        = string
  description = "Environment alias used in resource names"
}

variable "module_name" {
  type        = string
  description = "Module name used in resource names"
}

variable "prefix" {
  type        = string
  description = "Resource name prefix (product-env-module)"
}

variable "scheduled_task_config" {
  type = object({
    table_name          = optional(string, null)
    schedule_group_name = optional(string, null)
    enable_agent_tools  = optional(bool, false)
  })
  description = "Scheduled task configuration. Null names fall back to prefix-derived defaults."
  default     = {}
}

variable "create_scheduled_task_table" {
  type        = bool
  description = "Create a dedicated DynamoDB scheduled-task table. False when the session store is Redis/Valkey, where the existing cluster is reused with a separate keyspace and no new infrastructure is provisioned."
}

variable "input_queue_arn" {
  type        = string
  description = "ARN of the SQS input queue the timer delivers fires to"
}

variable "tags" {
  type        = map(string)
  description = "Tags applied to every scheduler resource"
  default     = {}
}
