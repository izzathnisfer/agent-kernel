from agentkernel.aws import ECSAgentRunner
from agentkernel.openai import OpenAIModule
from agents import Agent

# The agent needs no scheduling awareness. A fire arrives as an ordinary agent message, so
# this runs exactly as it would for a request that came in over HTTP.
#
# The scheduling tools (create/update/delete/list_scheduled_task) are attached automatically
# when scheduler.enabled is set and the deployment grants the runner scheduler access via
# scheduled_task_config.enable_agent_tools. They carry their own usage guidance, so the
# instructions below deliberately say nothing about them.
assistant_agent = Agent(
    name="assistant",
    instructions="You are a helpful assistant. Give short and direct answers.",
)

report_agent = Agent(
    name="report",
    instructions="You write brief status reports. Lead with the single most important fact, "
    "then at most three supporting bullets. Never pad a report to fill space.",
)

OpenAIModule([assistant_agent, report_agent])

# Agent Runner entrypoint - polls Input Queue, runs agent, sends to Output Queue.
handler = ECSAgentRunner.run

if __name__ == "__main__":
    handler()
