from typing import Optional

from agentkernel.api import RESTAPI, AgentRESTRequestHandler
from agentkernel.openai import OpenAIModule
from agentkernel.thread import Authoriser, ThreadRESTRequestHandler
from agents import Agent

general_agent = Agent(
    name="general",
    instructions="You provide assistance with general queries. Give short and clear answers",
)


class DemoAuthoriser(Authoriser):
    """
    Demo Authoriser protecting the thread read endpoints (GET /api/v1/threads*).

    A real subclass would validate the Bearer token against your own authentication
    provider (e.g. verify a JWT signature) and return the subject's user_id, or None
    to reject the request. Here a static token map stands in for that provider.
    """

    _TOKENS = {
        "alice-token": "alice",
        "bob-token": "bob",
    }

    def authorise(self, token: str) -> Optional[str]:
        return self._TOKENS.get(token)


OpenAIModule([general_agent])

if __name__ == "__main__":
    # The thread routes are served only because ThreadRESTRequestHandler is passed here —
    # a `thread` block in config.yaml configures storage alone. Dropping the authoriser
    # argument would leave those routes open.
    RESTAPI.run(handlers=[AgentRESTRequestHandler(), ThreadRESTRequestHandler(authoriser=DemoAuthoriser())])
