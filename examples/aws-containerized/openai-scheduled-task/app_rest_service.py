from typing import Optional

from agentkernel.api import RESTRequestHandler
from agentkernel.api.schedule import ScheduleRESTRequestHandler
from agentkernel.auth import Authoriser
from agentkernel.aws import AWSRestAPI, ECSIOHandler
from agentkernel.deployment.aws.containerized.core.api import ECSQueueRequestHandler


class DemoAuthoriser(Authoriser):
    """
    Demo Authoriser resolving a Bearer token to the user id that owns a scheduled task.

    A real subclass would validate the token against your own authentication provider
    (e.g. verify a JWT signature) and return the subject's user_id, or None to reject the
    request. Here a static token map stands in for that provider.

    Scheduling requires this. The owner is stamped server-side from whatever this returns and
    is never read from the request body, so a caller cannot forge it.
    """

    _TOKENS = {
        "alice-token": "alice",
        "bob-token": "bob",
    }

    def authorise(self, token: str) -> Optional[str]:
        return self._TOKENS.get(token)


def scheduling_handlers() -> list[RESTRequestHandler]:
    """Build the REST handlers, both carrying the Authoriser scheduling requires.

    ECSQueueRequestHandler serves POST /api/v1/chat, where a body carrying a `schedule`
    block creates a scheduled task instead of being enqueued. ScheduleRESTRequestHandler
    serves the /api/v1/schedule management routes. Both raise AKConfigError at startup if
    scheduling is enabled and no Authoriser is supplied, so neither can be omitted.

    Supplying our own ScheduleRESTRequestHandler also stops RESTAPI.run() from auto-mounting
    an unauthorised one.
    """
    authoriser = DemoAuthoriser()
    return [
        ECSQueueRequestHandler(authoriser=authoriser),
        ScheduleRESTRequestHandler(authoriser=authoriser),
    ]


# ECSIOHandler.run() starts AWSRestAPI with its default handlers, so overriding this
# classmethod is how an Authoriser gets supplied.
AWSRestAPI.get_default_handlers = classmethod(lambda cls: scheduling_handlers())

runner = ECSIOHandler.run

if __name__ == "__main__":
    runner()
