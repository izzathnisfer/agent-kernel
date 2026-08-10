import logging

from agentkernel.core.config import AKConfig
from agentkernel.core.model import ExecutionMode
from agentkernel.deployment.common import ThreadRunner

from .local_output_consumer import LocalOutputConsumer


class LocalIOHandler:
    """
    Local IO Handler — the local queue-mode entrypoint. Starts two peer threads via
    ThreadRunner: the REST API and the output-queue consumer.

    Thread 1 (rest-api) runs the FastAPI/uvicorn app: LocalQueueRequestHandler handles
    REST_SYNC/REST_ASYNC. Thread 2 (output-queue-consumer) runs LocalOutputConsumer.run,
    writing responses into LocalResponseStore.

    ASYNC/STREAM (WebSocket) execution modes are out of scope for local queue mode v1 —
    run() fails fast instead of starting a broken WebSocket path.
    """

    _log = logging.getLogger("ak.local.iohandler")
    _config = AKConfig.get()

    @classmethod
    def run(cls) -> None:
        mode = cls._config.execution.mode
        if mode in (ExecutionMode.ASYNC, ExecutionMode.STREAM):
            raise ValueError(
                f"LocalIOHandler only supports REST_SYNC/REST_ASYNC execution modes (mode={mode}); "
                "ASYNC/STREAM local queue mode support is future work."
            )

        cls._log.info(f"LocalIOHandler starting — mode={mode}")

        from .core.api.rest_api import LocalRestAPI

        def run_api() -> None:
            LocalRestAPI.run()

        ThreadRunner.run(
            tasks=[
                ThreadRunner.Task(
                    execution_function=run_api,  # a callable so ThreadRunner runs it in the thread, not here
                    thread_name="rest-api",
                    stop_all_on_failure=True,
                    graceful=True,
                    awaited_on_shutdown=False,  # uvicorn.run() isn't wired to shutdown_event and only stops via OS signal, so it can't report completion.
                ),
                ThreadRunner.Task(
                    execution_function=lambda: LocalOutputConsumer.run(), thread_name="output-queue-consumer", stop_all_on_failure=True
                ),
            ],
            max_workers=2,
        )
