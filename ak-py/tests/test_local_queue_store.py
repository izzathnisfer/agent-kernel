import time

import pytest

from testharness.core.queue_store import LocalQueue


@pytest.fixture
def queue(tmp_path):
    return LocalQueue(str(tmp_path / "queue.db"))


class TestEnqueueReceive:
    def test_receive_returns_boto3_shaped_record(self, queue):
        queue.enqueue("input", {"prompt": "hi", "session_id": "s1"}, attributes={"request_id": {"DataType": "String", "StringValue": "r1"}})

        [msg] = queue.receive("input", batch_size=10, visibility_timeout=30)

        assert msg["MessageId"] == "1"
        assert msg["ReceiptHandle"] == "1"
        assert msg["Body"] == '{"prompt": "hi", "session_id": "s1"}'
        assert msg["Attributes"]["ApproximateReceiveCount"] == "1"
        assert msg["MessageAttributes"] == {"request_id": {"DataType": "String", "StringValue": "r1"}}

    def test_message_group_and_dedup_ids_stored_and_returned(self, queue):
        queue.enqueue("input", {"prompt": "hi"}, message_group_id="g1", message_deduplication_id="d1")

        [msg] = queue.receive("input", batch_size=10, visibility_timeout=30)

        assert msg["Attributes"]["MessageGroupId"] == "g1"
        assert msg["Attributes"]["MessageDeduplicationId"] == "d1"

    def test_fifo_ordering(self, queue):
        for i in range(3):
            queue.enqueue("input", {"i": i})

        messages = queue.receive("input", batch_size=10, visibility_timeout=30)

        assert [m["MessageId"] for m in messages] == ["1", "2", "3"]

    def test_batch_size_limits_results(self, queue):
        for i in range(5):
            queue.enqueue("input", {"i": i})

        messages = queue.receive("input", batch_size=2, visibility_timeout=30)

        assert len(messages) == 2

    def test_queues_are_isolated_by_name(self, queue):
        queue.enqueue("input", {"i": "in"})
        queue.enqueue("output", {"i": "out"})

        input_messages = queue.receive("input", batch_size=10, visibility_timeout=30)
        output_messages = queue.receive("output", batch_size=10, visibility_timeout=30)

        assert len(input_messages) == 1
        assert len(output_messages) == 1
        assert input_messages[0]["Body"] == '{"i": "in"}'
        assert output_messages[0]["Body"] == '{"i": "out"}'


class TestInFlightTracking:
    def test_message_not_redelivered_while_in_flight(self, queue):
        queue.enqueue("input", {"i": 1})
        first = queue.receive("input", batch_size=10, visibility_timeout=30)
        assert len(first) == 1

        second = queue.receive("input", batch_size=10, visibility_timeout=30)
        assert second == []

    def test_message_redelivered_after_visibility_timeout_expires(self, queue):
        queue.enqueue("input", {"i": 1})
        first = queue.receive("input", batch_size=10, visibility_timeout=0.05)
        assert first[0]["Attributes"]["ApproximateReceiveCount"] == "1"

        time.sleep(0.1)

        second = queue.receive("input", batch_size=10, visibility_timeout=30)
        assert len(second) == 1
        assert second[0]["Attributes"]["ApproximateReceiveCount"] == "2"

    def test_delete_by_id_prevents_redelivery_even_after_timeout(self, queue):
        queue.enqueue("input", {"i": 1})
        [msg] = queue.receive("input", batch_size=10, visibility_timeout=0.05)

        queue.delete_by_id(int(msg["MessageId"]))
        time.sleep(0.1)

        assert queue.receive("input", batch_size=10, visibility_timeout=30) == []


class TestPersistence:
    def test_second_instance_same_file_sees_enqueued_message(self, tmp_path):
        db_path = str(tmp_path / "shared.db")
        writer = LocalQueue(db_path)
        writer.enqueue("input", {"i": 1})

        reader = LocalQueue(db_path)
        messages = reader.receive("input", batch_size=10, visibility_timeout=30)

        assert len(messages) == 1
