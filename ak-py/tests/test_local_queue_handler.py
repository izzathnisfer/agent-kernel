from unittest.mock import Mock

import pytest

from testharness.core.queue_handler import LocalQueueHandler


@pytest.fixture(autouse=True)
def _reset_handler_caches():
    LocalQueueHandler._queue = None
    LocalQueueHandler._config = None
    yield
    LocalQueueHandler._queue = None
    LocalQueueHandler._config = None


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "queue.db")
    config = Mock()
    config.execution.queues.input.url = path
    config.execution.queues.output.url = path
    monkeypatch.setattr(LocalQueueHandler, "_get_config", classmethod(lambda cls: config))
    return path


class TestSendMessageToInputQueue:
    def test_requires_prompt_and_session_id(self, db_path):
        with pytest.raises(Exception):
            LocalQueueHandler.send_message_to_input_queue({"agent": "x"})

    def test_defaults_message_group_id_to_session_id(self, db_path):
        LocalQueueHandler.send_message_to_input_queue({"prompt": "hi", "session_id": "s1"})

        [msg] = LocalQueueHandler.receive_messages("input", batch_size=10, visibility_timeout=30)
        assert msg["Attributes"]["MessageGroupId"] == "s1"

    def test_explicit_message_group_id_overrides_session_id(self, db_path):
        LocalQueueHandler.send_message_to_input_queue(
            {"prompt": "hi", "session_id": "s1"},
            attributes={"message_group_id": "g-override"},
        )

        [msg] = LocalQueueHandler.receive_messages("input", batch_size=10, visibility_timeout=30)
        assert msg["Attributes"]["MessageGroupId"] == "g-override"

    def test_request_id_and_user_id_become_custom_attributes(self, db_path):
        LocalQueueHandler.send_message_to_input_queue(
            {"prompt": "hi", "session_id": "s1"},
            request_id="req-1",
            user_id="user-1",
        )

        [msg] = LocalQueueHandler.receive_messages("input", batch_size=10, visibility_timeout=30)
        custom = LocalQueueHandler.get_message_custom_attributes(msg)
        assert custom == {"request_id": "req-1", "user_id": "user-1"}

    def test_body_lands_on_the_input_queue_not_output(self, db_path):
        LocalQueueHandler.send_message_to_input_queue({"prompt": "hi", "session_id": "s1"})

        assert LocalQueueHandler.receive_messages("output", batch_size=10, visibility_timeout=30) == []
        assert len(LocalQueueHandler.receive_messages("input", batch_size=10, visibility_timeout=30)) == 1


class TestSendMessageToOutputQueue:
    def test_defaults_message_group_id_from_body_session_id(self, db_path):
        LocalQueueHandler.send_message_to_output_queue({"result": "ok", "session_id": "s1"})

        [msg] = LocalQueueHandler.receive_messages("output", batch_size=10, visibility_timeout=30)
        assert msg["Attributes"]["MessageGroupId"] == "s1"

    def test_body_without_session_id_has_no_group_id(self, db_path):
        LocalQueueHandler.send_message_to_output_queue({"result": "ok"})

        [msg] = LocalQueueHandler.receive_messages("output", batch_size=10, visibility_timeout=30)
        assert msg["Attributes"]["MessageGroupId"] is None

    def test_duplicate_custom_attribute_names_rejected(self, db_path):
        dup = LocalQueueHandler.CustomAttribute(name="request_id", value="x", datatype=LocalQueueHandler.AttributeDataType.STRING)
        with pytest.raises(ValueError, match="Duplicate"):
            LocalQueueHandler.send_message_to_output_queue(
                {"result": "ok"},
                request_id="req-1",
                custom_message_attributes=[dup],
            )


class TestReceiveAndDeleteMessage:
    def test_receive_messages_wraps_local_queue_receive(self, db_path):
        LocalQueueHandler.send_message_to_input_queue({"prompt": "hi", "session_id": "s1"})

        messages = LocalQueueHandler.receive_messages("input", batch_size=10, visibility_timeout=30)

        assert len(messages) == 1

    def test_delete_message_removes_it_permanently(self, db_path):
        LocalQueueHandler.send_message_to_input_queue({"prompt": "hi", "session_id": "s1"})
        [msg] = LocalQueueHandler.receive_messages("input", batch_size=10, visibility_timeout=0.05)

        LocalQueueHandler.delete_message("input", msg["MessageId"])

        import time

        time.sleep(0.1)
        assert LocalQueueHandler.receive_messages("input", batch_size=10, visibility_timeout=30) == []

    def test_missing_queue_path_raises(self, monkeypatch):
        config = Mock()
        config.execution.queues.input.url = None
        config.execution.queues.output.url = None
        monkeypatch.setattr(LocalQueueHandler, "_get_config", classmethod(lambda cls: config))

        with pytest.raises(ValueError, match="execution.queues"):
            LocalQueueHandler.receive_messages("input", batch_size=10, visibility_timeout=30)


class TestAttributeExtractionHelpers:
    def test_get_message_system_attributes_returns_copy_of_attributes(self):
        record = {"Attributes": {"MessageGroupId": "g1", "ApproximateReceiveCount": "2"}}
        assert LocalQueueHandler.get_message_system_attributes(record) == {"MessageGroupId": "g1", "ApproximateReceiveCount": "2"}

    def test_get_message_system_attributes_defaults_to_empty_dict(self):
        assert LocalQueueHandler.get_message_system_attributes({}) == {}

    def test_get_message_custom_attributes_flattens_string_values(self):
        record = {"MessageAttributes": {"request_id": {"StringValue": "r1", "DataType": "String"}}}
        assert LocalQueueHandler.get_message_custom_attributes(record) == {"request_id": "r1"}

    def test_get_message_custom_attributes_skips_none_values(self):
        record = {"MessageAttributes": {"missing": {"StringValue": None, "DataType": "String"}}}
        assert LocalQueueHandler.get_message_custom_attributes(record) == {}
