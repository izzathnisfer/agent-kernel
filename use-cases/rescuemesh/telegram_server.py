from agentkernel.api import RESTAPI
from agentkernel.openai import OpenAIModule
from agentkernel.telegram import AgentTelegramRequestHandler

from agent import AGENTS

OpenAIModule(AGENTS)

if __name__ == "__main__":
    RESTAPI.run([AgentTelegramRequestHandler()])
