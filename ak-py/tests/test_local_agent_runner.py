import json
from unittest.mock import MagicMock, patch

import pytest

from testharness.core.queue_handler import LocalQueueHandler
from testharness.local_agent_runner import LocalAgentRunner


def _make_record(
    body: dict,
    message_id: str = "m1",
    message_group_id: str = "session-1",
    request_id: str = "req-1",
    user_id: str = "user-1",
):
    attrs = {}
    if request_id:
        attrs["request_id"] = {"StringValue": request_id, "DataType": "String"}
    if user_id:
        attrs["user_id"] = {"StringValue": user_id, "DataType": "String"}
    return {
        "MessageId": message_id,
        "Body": json.dumps(body),
        "Attributes": {"MessageGroupId": message_group_id},
        "MessageAttributes": attrs,
    }


class TestGetRecordAttributes:
    def test_extracts_all_fields(self):
        record = _make_record({"prompt": "hi", "session_id": "s1"})
        attrs = LocalAgentRunner._get_record_attributes(record)
        assert attrs == {
            "message_group_id": "session-1",
            "message_deduplication_id": None,
            "request_id": "req-1",
            "user_id": "user-1",
        }

    def test_raises_when_request_id_missing(self):
        record = _make_record({"prompt": "hi", "session_id": "s1"}, request_id=None)
        with pytest.raises(ValueError, match="request_id is required"):
            LocalAgentRunner._get_record_attributes(record)


class TestSendToOutputQueue:
    def test_forwards_group_dedup_request_user_ids(self):
        record_attributes = {
            "message_group_id": "session-1",
            "message_deduplication_id": "dedup-1",
            "request_id": "req-1",
            "user_id": "user-1",
        }

        with patch.object(LocalQueueHandler, "send_message_to_output_queue") as mock_send:
            LocalAgentRunner._send_to_output_queue({"result": "ok"}, record_attributes)

        mock_send.assert_called_once_with(
            message_body={"result": "ok"},
            attributes={"message_group_id": "session-1", "message_deduplication_id": "dedup-1"},
            request_id="req-1",
            user_id="user-1",
        )


class TestProcessMessage:
    def test_runs_chat_service_and_forwards_response(self):
        record = _make_record({"prompt": "hello", "session_id": "s1"})
        mock_chat_service = MagicMock()
        mock_chat_service.process_chat_request.return_value = (200, {"result": "ok", "session_id": "s1"})

        with (
            patch.object(LocalAgentRunner, "_get_chat_service", return_value=mock_chat_service),
            patch.object(LocalQueueHandler, "send_message_to_output_queue") as mock_send,
        ):
            LocalAgentRunner.process_message(record)

        mock_chat_service.process_chat_request.assert_called_once()
        mock_send.assert_called_once()
        assert mock_send.call_args.kwargs["message_body"] == {"result": "ok", "session_id": "s1"}
        assert mock_send.call_args.kwargs["request_id"] == "req-1"


class TestOnPermanentFailure:
    def test_sends_error_body_to_output_queue(self):
        record = _make_record({"prompt": "hello", "session_id": "s1"})

        with patch.object(LocalQueueHandler, "send_message_to_output_queue") as mock_send:
            LocalAgentRunner.on_permanent_failure(record)

        mock_send.assert_called_once()
        error_body = mock_send.call_args.kwargs["message_body"]
        assert "error" in error_body

    def test_catches_own_exceptions(self):
        """on_permanent_failure must never raise — LocalQueueConsumer relies on this to delete the message."""
        bad_record = {"MessageId": "m1", "Body": "not json", "Attributes": {}, "MessageAttributes": {}}
        LocalAgentRunner.on_permanent_failure(bad_record)
