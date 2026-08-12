from agentkernel.aws import ECSAgentRunner
from agentkernel.openai import OpenAIModule
from agents import Agent

# A scheduled fire arrives as an ordinary agent message, so the agent needs no scheduling
# awareness. Scheduling tools are attached automatically and carry their own usage guidance.
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
