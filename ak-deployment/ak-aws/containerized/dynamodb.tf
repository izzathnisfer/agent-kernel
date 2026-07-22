# ---------- DynamoDB Response Store ----------

resource "aws_dynamodb_table" "response_store" {
  count = var.queue_mode ? 1 : 0

  name         = "${local.prefix}-response-store"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "request_id"

  attribute {
    name = "request_id"
    type = "S"
  }

  ttl {
    attribute_name = "expiry_time"
    enabled        = true
  }

  tags = merge(var.tags, { Type = "ResponseStore" })
}

# ---------- DynamoDB Session ID Mapping (agent-initiated conversations) ----------
# The mapping store follows session.type at the application level, so this table is only
# used when the session store itself is DynamoDB (create_dynamodb_memory_table = true).
# Set conversation_initiation accordingly for other session backends (Redis, Valkey, ...).

resource "aws_dynamodb_table" "session_id_mapping" {
  count = var.conversation_initiation ? 1 : 0

  name         = "${local.prefix}-session-id-mapping"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "map_key"

  attribute {
    name = "map_key"
    type = "S"
  }

  ttl {
    attribute_name = "expiry_time"
    enabled        = true
  }

  tags = merge(var.tags, { Type = "SessionIdMapping" })
}
