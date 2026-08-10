import json
from unittest.mock import MagicMock, patch

import pytest

from testharness.local_output_consumer import LocalOutputConsumer


def _make_record(
    body,
    message_id: str = "m1",
    message_group_id: str = "session-1",
    request_id: str = "req-1",
):
    attrs = {}
    if request_id:
        attrs["request_id"] = {"StringValue": request_id, "DataType": "String"}
    return {
        "MessageId": message_id,
        "Body": json.dumps(body) if not isinstance(body, str) else body,
        "Attributes": {"MessageGroupId": message_group_id},
        "MessageAttributes": attrs,
    }


@pytest.fixture(autouse=True)
def _reset_response_store():
    LocalOutputConsumer._response_store = None
    yield
    LocalOutputConsumer._response_store = None


class TestConstructMessageForStore:
    def test_uses_record_body_by_default(self):
        record = _make_record({"result": "ok", "session_id": "s1"})
        message = LocalOutputConsumer._construct_message_for_store(record)
        assert message == {"session_id": "s1", "request_id": "req-1", "body": {"result": "ok", "session_id": "s1"}}

    def test_override_body_takes_precedence(self):
        record = _make_record({"result": "ok", "session_id": "s1"})
        override = json.dumps({"error": "boom", "session_id": "s1"})
        message = LocalOutputConsumer._construct_message_for_store(record, body=override)
        assert message["body"] == {"error": "boom", "session_id": "s1"}

    def test_raises_when_request_id_missing(self):
        record = _make_record({"result": "ok"}, request_id=None)
        with pytest.raises(ValueError, match="request_id is required"):
            LocalOutputConsumer._construct_message_for_store(record)


class TestProcessMessage:
    def test_writes_message_to_response_store(self):
        record = _make_record({"result": "ok", "session_id": "s1"})
        mock_store = MagicMock()

        with patch.object(LocalOutputConsumer, "_get_response_store", return_value=mock_store):
            LocalOutputConsumer.process_message(record)

        mock_store.add_message.assert_called_once_with({"session_id": "s1", "request_id": "req-1", "body": {"result": "ok", "session_id": "s1"}})


class TestOnPermanentFailure:
    def test_stores_error_entry_with_session_id(self):
        record = _make_record({"result": "ok", "session_id": "s1"})
        mock_store = MagicMock()

        with patch.object(LocalOutputConsumer, "_get_response_store", return_value=mock_store):
            LocalOutputConsumer.on_permanent_failure(record)

        mock_store.add_message.assert_called_once()
        stored = mock_store.add_message.call_args.args[0]
        assert stored["body"]["error"] is not None
        assert stored["session_id"] == "session-1"

    def test_catches_own_exceptions(self):
        """on_permanent_failure must never raise — LocalQueueConsumer relies on this to delete the message."""
        bad_record = {"MessageId": "m1", "Body": "not json", "Attributes": {}, "MessageAttributes": {}}
        LocalOutputConsumer.on_permanent_failure(bad_record)
