from agentkernel.aws import Lambda

# Hosts POST /api/v1/chat — which creates a scheduled task when the body carries a
# `schedule` block — and the /api/v1/schedule management routes, which the deployment
# mounts automatically when scheduled_task = true.
#
# There is no scheduling code here. The owner of a scheduled task comes from the API
# Gateway authorizer's principalId (see lambda_auth.py), which Agent Kernel reads off the
# event; a request that arrives without it is rejected with 401.
handler = Lambda.handler
