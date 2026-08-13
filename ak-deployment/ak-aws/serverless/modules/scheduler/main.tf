# Scheduled Tasks: the scheduled-task table (DynamoDB stores only), the schedule group, and
# the timer's execution role. Component IAM grants for these resources live with the components.

locals {
  table_name          = coalesce(var.scheduled_task_config.table_name, "${var.prefix}-scheduled-tasks")
  schedule_group_name = coalesce(var.scheduled_task_config.schedule_group_name, "${var.prefix}-schedules")
}

# ---------- Scheduled-task table ----------
# Created only when the session store is DynamoDB. With Redis/Valkey sessions the existing
# cluster is reused with a separate keyspace, so this module provisions nothing here.

resource "aws_dynamodb_table" "scheduled_tasks" {
  count = var.create_scheduled_task_table ? 1 : 0

  name         = local.table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "scheduled_task_id"

  attribute {
    name = "scheduled_task_id"
    type = "S"
  }

  # Sparse index key: it mirrors owner_id while the row is live and is removed on
  # soft-delete, so tombstones drop out of the listing without a filter expression.
  attribute {
    name = "owner_index_key"
    type = "S"
  }

  attribute {
    name = "created_at"
    type = "S"
  }

  global_secondary_index {
    name            = "owner-index"
    hash_key        = "owner_index_key"
    range_key       = "created_at"
    projection_type = "ALL"
  }

  # Expires soft-deleted rows after their grace window; live rows carry no expiry_time.
  ttl {
    attribute_name = "expiry_time"
    enabled        = true
  }

  tags = merge(var.tags, { Type = "ScheduledTaskStore" })
}

# ---------- Schedule group ----------
# One per deployment, for namespacing and destroy-time cleanup: deleting the group removes
# every registration the deployment created, so terraform destroy leaves no orphans behind.

resource "aws_scheduler_schedule_group" "this" {
  name = local.schedule_group_name
  tags = merge(var.tags, { Type = "ScheduleGroup" })
}

# A schedule's ARN is .../schedule/<group>/<name>, not a child of the group's own ARN
# (.../schedule-group/<group>), so component grants must use the pattern derived below.

locals {
  schedule_arn_pattern = "${replace(aws_scheduler_schedule_group.this.arn, ":schedule-group/", ":schedule/")}/*"
}

# ---------- Timer execution role ----------
# Assumed by EventBridge Scheduler when a schedule fires. Its only permission is to send to
# the input queue.

resource "aws_iam_role" "scheduler_target" {
  name = "${var.prefix}-scheduler-target-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "scheduler.amazonaws.com" }
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy" "scheduler_target" {
  name = "${var.prefix}-scheduler-target-policy"
  role = aws_iam_role.scheduler_target.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["sqs:SendMessage"]
      Resource = var.input_queue_arn
    }]
  })
}
