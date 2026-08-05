from agentkernel.api import RESTAPI
from agentkernel.openai import OpenAIModule
from agentkernel.slack import AgentSlackRequestHandler
from agentkernel.telegram import AgentTelegramRequestHandler
from agents import Agent as OpenAIAgent

general_agent = OpenAIAgent(
    name="general",
    handoff_description="Agent for general questions",
    instructions="You are an integration test agent. Reply to every message with a short, one-sentence answer.",
    model="openai/gpt-4.1-mini",
)

OpenAIModule([general_agent])


def main():
    RESTAPI.run([AgentSlackRequestHandler(), AgentTelegramRequestHandler()])


if __name__ == "__main__":
    main()
