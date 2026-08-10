from unittest.mock import MagicMock, patch

import pytest

from agentkernel.deployment.common.thread_runner import ThreadRunner
from testharness.core.queue_consumer import LocalQueueConsumer


def _make_msg(message_id, receive_count=1):
    return {
        "MessageId": message_id,
        "ReceiptHandle": message_id,
        "Body": "{}",
        "Attributes": {"ApproximateReceiveCount": str(receive_count)},
    }


class _SyncConsumer(LocalQueueConsumer):
    process_message = MagicMock()
    on_permanent_failure = MagicMock()
    delete_message = MagicMock()

    @classmethod
    def get_queue_name(cls):
        return "input"


class _AsyncConsumer(LocalQueueConsumer):
    on_permanent_failure = MagicMock()
    delete_message = MagicMock()

    @classmethod
    async def process_message(cls, record):
        pass

    @classmethod
    def get_queue_name(cls):
        return "input"


@pytest.fixture(autouse=True)
def reset_mocks():
    ThreadRunner.shutdown_event.clear()
    _SyncConsumer.process_message.reset_mock()
    _SyncConsumer.on_permanent_failure.reset_mock()
    _SyncConsumer.delete_message.reset_mock()
    _AsyncConsumer.on_permanent_failure.reset_mock()
    _AsyncConsumer.delete_message.reset_mock()
    yield
    ThreadRunner.shutdown_event.clear()


class TestNumConsumersDefault:
    def test_base_class_default(self):
        assert LocalQueueConsumer.num_consumers == 1


class TestProcessSingle:
    def test_processes_and_deletes_message(self):
        msg = _make_msg("m1")
        _SyncConsumer._process_single(msg)
        _SyncConsumer.process_message.assert_called_once_with(msg)
        _SyncConsumer.delete_message.assert_called_once_with(msg)

    def test_message_exceeds_max_receive_count(self):
        msg = _make_msg("m1", receive_count=_SyncConsumer.max_receive_count + 1)
        _SyncConsumer._process_single(msg)
        _SyncConsumer.on_permanent_failure.assert_called_once_with(msg)
        _SyncConsumer.process_message.assert_not_called()
        _SyncConsumer.delete_message.assert_called_once_with(msg)

    def test_process_message_raises_does_not_delete(self):
        _SyncConsumer.process_message.side_effect = RuntimeError("boom")
        msg = _make_msg("m1")
        _SyncConsumer._process_single(msg)
        _SyncConsumer.delete_message.assert_not_called()
        _SyncConsumer.process_message.side_effect = None

    def test_async_process_message_is_run_and_deleted(self):
        msg = _make_msg("async-msg")
        _AsyncConsumer._process_single(msg)
        _AsyncConsumer.delete_message.assert_called_once_with(msg)


class TestDeleteMessage:
    def test_delete_message_delegates_to_handler(self):
        from testharness.core.queue_handler import LocalQueueHandler

        class _Consumer(LocalQueueConsumer):
            @classmethod
            def get_queue_name(cls):
                return "output"

        with patch.object(LocalQueueHandler, "delete_message") as mock_delete:
            _Consumer.delete_message({"MessageId": "42"})

        mock_delete.assert_called_once_with("output", "42")


class TestConsumerLoop:
    def test_stops_after_poll_raises_once_processed_batch(self):
        msg = _make_msg("m1")
        poll_results = [[msg], RuntimeError("stop")]

        def fake_poll():
            result = poll_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        with (
            patch.object(_SyncConsumer, "poll", side_effect=fake_poll),
            patch(
                "testharness.core.queue_consumer.time.sleep",
                side_effect=RuntimeError("stop-loop"),
            ),
        ):
            with pytest.raises(RuntimeError, match="stop-loop"):
                _SyncConsumer._consumer_loop()

        _SyncConsumer.process_message.assert_called_once_with(msg)
        _SyncConsumer.delete_message.assert_called_once_with(msg)

    def test_empty_poll_sleeps_instead_of_busy_looping(self):
        poll_results = [[], RuntimeError("stop")]

        def fake_poll():
            result = poll_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        with (
            patch.object(_SyncConsumer, "poll", side_effect=fake_poll),
            patch("testharness.core.queue_consumer.time.sleep", side_effect=RuntimeError("stop-loop")) as mock_sleep,
        ):
            with pytest.raises(RuntimeError, match="stop-loop"):
                _SyncConsumer._consumer_loop()

        mock_sleep.assert_called_once_with(_SyncConsumer.empty_poll_sleep)


class TestRun:
    def test_run_rejects_zero_consumers(self):
        class _ZeroConsumer(LocalQueueConsumer):
            num_consumers = 0

            @classmethod
            def get_queue_name(cls):
                return "input"

            @classmethod
            def process_message(cls, record):
                pass

            @classmethod
            def on_permanent_failure(cls, record):
                pass

        with pytest.raises(ValueError, match="num_consumers must be >= 1"):
            _ZeroConsumer.run()
