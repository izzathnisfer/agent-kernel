output "agent_invoke_url" {
  description = "The URL to invoke the agent Cloud Run service"
  value       = module.serverless_agents.agent_invoke_url
}

output "network_id" {
  description = "VPC network id of this (base) deployment"
  value       = module.serverless_agents.network_id
}

output "private_subnet_id" {
  description = "Private subnet id of this (base) deployment"
  value       = module.serverless_agents.private_subnet_id
}
