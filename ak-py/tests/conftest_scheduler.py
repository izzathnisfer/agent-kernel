"""Shared fixtures and builders for the scheduled-task tests.

Imported explicitly by the scheduler test modules rather than living in ``conftest.py``,
so the rest of the suite is unaffected.
"""

import json
from typing import Optional
from unittest.mock import MagicMock

from agentkernel.core.config import (
    AKConfig,
    _SchedulerConfig,
    _SchedulerDynamoDBConfig,
    _SchedulerRedisConfig,
)
from agentkernel.scheduler.factory import SchedulerFactory
from agentkernel.scheduler.providers.aws import AWSScheduler
from agentkernel.scheduler.testing import InMemoryScheduledTaskStore

FIFO_INPUT_URL = "https://sqs.us-east-1.amazonaws.com/1/input.fifo"
OUTPUT_URL = "https://sqs.us-east-1.amazonaws.com/1/output.fifo"


def enable_scheduler_config(
    *,
    session_type: str = "dynamodb",
    input_url: str = FIFO_INPUT_URL,
    output_url: str = OUTPUT_URL,
    group_name: Optional[str] = "grp",
    target_role_arn: Optional[str] = "arn:aws:iam::1:role/timer",
    dynamodb: Optional[_SchedulerDynamoDBConfig] = None,
    redis: Optional[_SchedulerRedisConfig] = None,
    valkey=None,
) -> AKConfig:
    """Put the live AKConfig into a scheduler-enabled state.

    :return: The mutated singleton, so a test can tweak it further.
    """
    config = AKConfig.get()
    config.session.type = session_type
    config.execution.queues.input.url = input_url
    config.execution.queues.output.url = output_url
    config.scheduler = _SchedulerConfig(
        enabled=True,
        group_name=group_name,
        target_role_arn=target_role_arn,
        dynamodb=dynamodb if dynamodb is not None else (_SchedulerDynamoDBConfig() if session_type == "dynamodb" else None),
        redis=redis if redis is not None else (_SchedulerRedisConfig() if session_type == "redis" else None),
        valkey=valkey,
    )
    return config


def reset_scheduler_config(previous_session_type: str = "in_memory") -> None:
    """Undo :func:`enable_scheduler_config` and drop the memoized scheduler."""
    config = AKConfig.get()
    config.scheduler = None
    config.session.type = previous_session_type
    config.execution.queues.input.url = None
    config.execution.queues.output.url = None
    SchedulerFactory._reset()


def make_sqs_client(visibility_timeout: int = 60, max_receive_count: Optional[int] = 5) -> MagicMock:
    """Build a mocked SQS client answering GetQueueAttributes."""
    attributes = {"VisibilityTimeout": str(visibility_timeout)}
    if max_receive_count is not None:
        attributes["RedrivePolicy"] = json.dumps({"maxReceiveCount": max_receive_count})
    client = MagicMock()
    client.get_queue_attributes.return_value = {"Attributes": attributes}
    return client


def make_scheduler(store: Optional[InMemoryScheduledTaskStore] = None, input_queue_url: Optional[str] = FIFO_INPUT_URL, **sqs_kwargs) -> AWSScheduler:
    """Build an AWSScheduler over mocked AWS clients and an in-memory store.

    :param input_queue_url: The fire target; pass a blank value to model a component that was
        never given ``execution.queues.input.url``.
    """
    return AWSScheduler(
        group_name="grp",
        target_role_arn="arn:aws:iam::1:role/timer",
        input_queue_url=input_queue_url,
        store=store if store is not None else InMemoryScheduledTaskStore(),
        scheduler_client=MagicMock(),
        sqs_client=make_sqs_client(**sqs_kwargs),
    )


def install_scheduler(store: Optional[InMemoryScheduledTaskStore] = None) -> AWSScheduler:
    """Seed the factory's memoized scheduler so components resolve the fake one.

    Components construct their service through ``SchedulerFactory`` in ``__init__``, so the
    fake has to be in place before they are built — not swapped in afterwards.

    :return: The installed scheduler.
    """
    scheduler = make_scheduler(store)
    SchedulerFactory._instance = scheduler
    return scheduler
