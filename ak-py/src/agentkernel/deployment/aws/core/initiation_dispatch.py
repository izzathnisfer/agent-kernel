"""
AWS queue dispatcher for agent-initiated conversations.

Registered by the agent runners (ECS and Lambda) at startup: InitiationMessages
produced by the initiate_conversation tool are enqueued to the Output Queue,
marked with the INITIATION message-type attribute, for the response handler's
process_message override to deliver.
"""

import logging

from ....core.initiation import INITIATION_MESSAGE_TYPE, InitiationManager, InitiationMessage
from .sqs_handler import SQSHandler


class InitiationQueueDispatcher:
    """
    Output Queue dispatcher for agent-initiated conversations. Registered by the
    agent runner entry points (ECS and Lambda) at startup.
    """

    _log = logging.getLogger("ak.aws.initiation")

    @classmethod
    def register(cls) -> None:
        """
        Register the Output Queue dispatcher for agent-initiated conversations.
        Called by the agent runner entry points; safe to call whether or not the
        feature is enabled, and idempotent.
        """
        InitiationManager.register_dispatcher(cls._dispatch)

    @classmethod
    def _dispatch(cls, initiation: InitiationMessage) -> None:
        """
        Send an InitiationMessage to the Output Queue with the standard attribute
        contract (request_id/user_id) plus the INITIATION message-type marker.

        :param initiation: The InitiationMessage to enqueue.
        """
        cls._log.info(f"Enqueuing initiation for session {initiation.session_id} to the Output Queue")
        SQSHandler.send_message_to_output_queue(
            message_body=initiation.model_dump(),
            attributes={"message_group_id": initiation.session_id, "message_deduplication_id": initiation.request_id},
            request_id=initiation.request_id,
            user_id=initiation.user_id,
            custom_message_attributes=[
                SQSHandler.CustomAttribute(name="message_type", value=INITIATION_MESSAGE_TYPE, datatype=SQSHandler.AttributeDataType.STRING)
            ],
        )
