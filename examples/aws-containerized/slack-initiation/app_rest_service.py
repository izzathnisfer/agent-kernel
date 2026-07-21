"""
REST Service (ECS) — runs the Slack webhook receiver and the output-queue
poller as peer threads.

This mirrors ECSIOHandler.run()'s own ~15-line thread-wiring pattern
(ak-py/src/agentkernel/deployment/aws/containerized/ecs_io_handler.py)
directly, rather than extending that class, since ECSIOHandler hardcodes
ECSQueueRequestHandler + ECSOutputConsumer with no way to add a Slack handler
or swap the output consumer — replicating its small wiring here needs no
changes to the agentkernel library.

Thread 1 (rest-api): FastAPI/uvicorn serving /health and /slack/events
                     (SlackECSRequestHandler).
Thread 2 (output-queue-consumer): SlackECSOutputConsumer.run() — polls the
                     Output Queue and delivers replies to Slack.
"""

from agentkernel.api import RESTAPI
from agentkernel.deployment.common import ThreadRunner

from slack_output_consumer import SlackECSOutputConsumer
from slack_request_handler import SlackECSRequestHandler


def run() -> None:
    ThreadRunner.run(
        tasks=[
            ThreadRunner.Task(
                execution_function=lambda: RESTAPI.run(handlers=[SlackECSRequestHandler()]),
                thread_name="rest-api",
                stop_all_on_failure=True,
                graceful=True,
                awaited_on_shutdown=False,
            ),
            ThreadRunner.Task(
                execution_function=lambda: SlackECSOutputConsumer.run(),
                thread_name="output-queue-consumer",
                stop_all_on_failure=True,
            ),
        ],
        max_workers=2,
    )


if __name__ == "__main__":
    run()
