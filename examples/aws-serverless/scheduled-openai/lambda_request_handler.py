from agentkernel.aws import Lambda

# Hosts POST /api/v1/chat, which creates a scheduled task when the body carries a `schedule`
# block, plus the /api/v1/schedule management routes the deployment mounts automatically when
# scheduled_task = true.
#
# No scheduling code is needed here. A task's owner comes from the API Gateway authorizer's
# principalId (see lambda_auth.py), which Agent Kernel reads off the event. A request without
# it is rejected with 401.
handler = Lambda.handler
