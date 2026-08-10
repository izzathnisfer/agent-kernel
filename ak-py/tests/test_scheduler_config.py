"""Enablement checks and the soft-delete TTL derivation."""

import pytest
from conftest_scheduler import FIFO_INPUT_URL, enable_scheduler_config, make_scheduler, reset_scheduler_config

from agentkernel.core.config import AKConfig, _SchedulerDynamoDBConfig, _SchedulerRedisConfig, _SchedulerValkeyConfig
from agentkernel.core.util.factory import AKConfigError
from agentkernel.scheduler.errors import SchedulerError
from agentkernel.scheduler.factory import SchedulerFactory
from agentkernel.scheduler.providers.aws import TTL_FLOOR_SECONDS, TTL_SAFETY_MARGIN_SECONDS


@pytest.fixture(autouse=True)
def _clean_config():
    yield
    reset_scheduler_config()


def test_enabled_is_false_when_the_block_is_absent():
    AKConfig.get().scheduler = None
    assert SchedulerFactory.enabled() is False


def test_validate_config_is_a_no_op_when_disabled():
    AKConfig.get().scheduler = None
    SchedulerFactory.validate_config()  # must not raise


def test_valid_dynamodb_configuration_passes():
    enable_scheduler_config()
    SchedulerFactory.validate_config()


def test_valid_redis_configuration_passes():
    enable_scheduler_config(session_type="redis")
    SchedulerFactory.validate_config()


@pytest.mark.parametrize("missing", ["input", "output"])
def test_missing_queue_url_is_rejected(missing):
    enable_scheduler_config()
    setattr(getattr(AKConfig.get().execution.queues, missing), "url", None)
    with pytest.raises(AKConfigError, match="queue mode"):
        SchedulerFactory.validate_config()


def test_non_fifo_input_queue_is_rejected():
    enable_scheduler_config(input_url="https://sqs.us-east-1.amazonaws.com/1/input")
    with pytest.raises(AKConfigError, match="FIFO"):
        SchedulerFactory.validate_config()


@pytest.mark.parametrize("session_type", ["in_memory", "cosmosdb", "firestore", "my.custom.SessionStore"])
def test_non_durable_session_store_is_rejected(session_type):
    enable_scheduler_config(session_type=session_type)
    with pytest.raises(AKConfigError, match="durable session store"):
        SchedulerFactory.validate_config()


@pytest.mark.parametrize("field", ["group_name", "target_role_arn"])
@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_or_blank_timer_wiring_is_rejected(field, value):
    enable_scheduler_config(**{field: value})
    with pytest.raises(AKConfigError, match=field):
        SchedulerFactory.validate_config()


def test_missing_backend_block_is_rejected():
    enable_scheduler_config(dynamodb=_SchedulerDynamoDBConfig(table_name=""))
    with pytest.raises(AKConfigError, match="scheduler.dynamodb.table_name"):
        SchedulerFactory.validate_config()


def test_backend_block_for_another_session_type_is_rejected():
    """A configured-but-unread table is worse silently ignored than loudly rejected."""
    enable_scheduler_config(session_type="redis", dynamodb=_SchedulerDynamoDBConfig(table_name="ak-scheduled-tasks"))
    with pytest.raises(AKConfigError, match="never be read"):
        SchedulerFactory.validate_config()


def test_valkey_block_on_a_dynamodb_deployment_is_rejected():
    enable_scheduler_config(valkey=_SchedulerValkeyConfig())
    with pytest.raises(AKConfigError, match="never be read"):
        SchedulerFactory.validate_config()


def test_redis_deployment_requires_its_prefix():
    enable_scheduler_config(session_type="redis", redis=_SchedulerRedisConfig(prefix=""))
    with pytest.raises(AKConfigError, match="scheduler.redis.prefix"):
        SchedulerFactory.validate_config()


class TestSoftDeleteTTL:
    """The TTL sizes the window during which a deleted task's id stays reserved."""

    def test_queue_redrive_count_wins_when_it_is_higher(self):
        enable_scheduler_config()
        AKConfig.get().execution.queues.input.max_receive_count = 3
        scheduler = make_scheduler(visibility_timeout=600, max_receive_count=5)
        assert scheduler.soft_delete_ttl_seconds == 600 * 5 + TTL_SAFETY_MARGIN_SECONDS

    def test_config_receive_count_wins_when_it_is_higher(self):
        enable_scheduler_config()
        AKConfig.get().execution.queues.input.max_receive_count = 8
        scheduler = make_scheduler(visibility_timeout=600, max_receive_count=2)
        assert scheduler.soft_delete_ttl_seconds == 600 * 8 + TTL_SAFETY_MARGIN_SECONDS

    def test_absent_redrive_policy_falls_back_to_the_config_value(self):
        """The default deployment has no DLQ, so the queue carries no maxReceiveCount."""
        enable_scheduler_config()
        AKConfig.get().execution.queues.input.max_receive_count = 4
        scheduler = make_scheduler(visibility_timeout=600, max_receive_count=None)
        assert scheduler.soft_delete_ttl_seconds == 600 * 4 + TTL_SAFETY_MARGIN_SECONDS

    def test_ttl_is_floored(self):
        enable_scheduler_config()
        AKConfig.get().execution.queues.input.max_receive_count = 1
        scheduler = make_scheduler(visibility_timeout=30, max_receive_count=1)
        assert scheduler.soft_delete_ttl_seconds == TTL_FLOOR_SECONDS

    def test_unreadable_queue_attributes_raise_rather_than_guess(self):
        enable_scheduler_config()
        from unittest.mock import MagicMock

        from agentkernel.scheduler.providers.aws import AWSScheduler
        from agentkernel.scheduler.testing import InMemoryScheduledTaskStore

        sqs = MagicMock()
        sqs.get_queue_attributes.side_effect = RuntimeError("access denied")
        with pytest.raises(SchedulerError, match="soft-delete TTL"):
            AWSScheduler("grp", "arn:role", FIFO_INPUT_URL, InMemoryScheduledTaskStore(), scheduler_client=MagicMock(), sqs_client=sqs)


def test_env_vars_alone_populate_the_absent_scheduler_block(monkeypatch):
    """AK_SCHEDULER__* fills the whole optional block, nested sub-blocks included.

    This is what lets Terraform supply the deployment values; the examples still declare a
    placeholder block for readability, not because it is required.
    """
    for name, value in {
        "AK_SCHEDULER__ENABLED": "true",
        "AK_SCHEDULER__GROUP_NAME": "grp",
        "AK_SCHEDULER__TARGET_ROLE_ARN": "arn:aws:iam::1:role/timer",
        "AK_SCHEDULER__DYNAMODB__TABLE_NAME": "ak-scheduled-tasks",
    }.items():
        monkeypatch.setenv(name, value)
    AKConfig._reset()
    try:
        scheduler = AKConfig.get().scheduler
        assert scheduler is not None
        assert scheduler.enabled is True
        assert scheduler.group_name == "grp"
        assert scheduler.dynamodb.table_name == "ak-scheduled-tasks"
    finally:
        AKConfig._reset()
