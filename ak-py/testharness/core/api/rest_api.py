from agentkernel.api.handler import RESTRequestHandler
from agentkernel.api.http import RESTAPI
from agentkernel.deployment.common.queue_handler import QueueHandler
from agentkernel.deployment.common.rest_handler import RestHandler

from ..queue_handler import LocalQueueHandler
from ..response_store import LocalResponseStore


class LocalQueueRequestHandler(RestHandler):
    """SQLite RestHandler; bypasses ChatService (validation/execution happen in LocalAgentRunner)."""

    def __init__(self):
        super().__init__(logger_name="ak.local.queue_handler")
        self._response_store = None
        self._queue_handler = None

    def get_response_store(self):
        """Lazily create the response store."""
        if self._response_store is None:
            self._response_store = LocalResponseStore(self._config.execution.queues.output.url)
        return self._response_store

    def get_queue_handler(self) -> QueueHandler:
        """Lazily resolve the queue handler."""
        if self._queue_handler is None:
            self._queue_handler = LocalQueueHandler
        return self._queue_handler


class LocalRestAPI(RESTAPI):
    """REST API for local queue mode deployments; defaults to LocalQueueRequestHandler so requests are enqueued to the local SQLite queue rather than run inline."""

    @classmethod
    def get_default_handlers(cls) -> list[RESTRequestHandler]:
        return [LocalQueueRequestHandler()]
