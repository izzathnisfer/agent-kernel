from typing import Optional

from agentkernel.api import RESTRequestHandler
from agentkernel.api.schedule import ScheduleRESTRequestHandler
from agentkernel.aws import AWSRestAPI, ECSIOHandler
from agentkernel.core.thread import Authoriser
from agentkernel.deployment.aws.containerized.core.api import ECSQueueRequestHandler


class DemoAuthoriser(Authoriser):
    """
    Demo Authoriser resolving a Bearer token to the user id that owns a scheduled task.

    A real subclass would validate the token against your own authentication provider
    (e.g. verify a JWT signature) and return the subject's user_id, or None to reject the
    request. Here a static token map stands in for that provider.

    Scheduling requires this: every scheduled task is owned by an authenticated identity,
    and the owner is stamped server-side from whatever this returns — it is never read
    from the request body, so it cannot be forged.
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


# ECSIOHandler.run() starts AWSRestAPI with its default handlers, so this classmethod is
# the seam where an Authoriser gets supplied.
AWSRestAPI.get_default_handlers = classmethod(lambda cls: scheduling_handlers())

runner = ECSIOHandler.run

if __name__ == "__main__":
    runner()
