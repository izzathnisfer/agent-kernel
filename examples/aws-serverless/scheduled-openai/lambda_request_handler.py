from agentkernel.aws import Lambda

# Hosts POST /api/v1/chat (creates a scheduled task when the body has a `schedule` block) plus
# the auto-mounted /api/v1/schedule management routes. The task owner comes from the API
# Gateway authorizer's principalId (see lambda_auth.py); a request without it gets a 401.
handler = Lambda.handler
