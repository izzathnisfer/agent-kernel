"""
Data models for agent-initiated conversations.

``INITIATION_MESSAGE_TYPE`` is the custom queue-message attribute value that marks an
InitiationMessage on the Output Queue; response handlers branch on it before their normal
processing.
"""

from typing import Literal, Optional

from pydantic import BaseModel

INITIATION_MESSAGE_TYPE = "INITIATION"


class InitiationMessage(BaseModel):
    """
    InitiationMessage carries an agent-initiated outbound message from the Agent
    Runner (where the session is created) to the Response Handler (the single
    send point, where the messaging platform thread id becomes known).

    session_id: str : New session id created by the initiation tool inside the runner
    message: str : Agent-generated outbound text
    target: str : Opaque recipient address — never interpreted by the core
    target_details: Optional[dict] : Opaque platform extras for the sender override
    user_id: str : Recipient id — owns the AK conversation thread; defaults to target
    request_id: str : Fresh unique id satisfying the output-queue attribute contract
    type: Literal["initiation"]
    """

    session_id: str
    message: str
    target: str
    target_details: Optional[dict] = None
    user_id: str
    request_id: str
    type: Literal["initiation"] = "initiation"
