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
# used when the session store itself is DynamoDB (create_dynamodb_memory_table = true) —
# enforced by the validation on var.conversation_initiation. Other session backends
# (Redis, Valkey, ...) need no resource here: the mapping rides the session store.
#
# The name MUST stay in lockstep with the application's derivation: the mapping store
# suffixes the session store's own table name with "-id-mapping"
# (core/initiation/mapping/dynamodb.py). local.dynamodb_memory_table_name is exactly what
# is injected as AK_SESSION__DYNAMODB__TABLE_NAME, so deriving from it here keeps the two
# names paired by construction rather than by a literal that can drift.

resource "aws_dynamodb_table" "session_id_mapping" {
  count = var.conversation_initiation ? 1 : 0

  name         = "${local.dynamodb_memory_table_name}-id-mapping"
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
