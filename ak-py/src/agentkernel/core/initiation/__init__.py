"""
Agent-initiated conversations.

Provides the InitiationManager façade that resolves and binds Session ID Mappings and
dispatches initiation messages, and the InitiationSender / SessionIdResolver contracts for
the handler roles. The mapping store itself belongs to the session store — see
``agentkernel.core.session.MappingStore`` and ``SessionStore.get_mapping_store()``.
"""

from .manager import InitiationManager, InitiationSender, SessionIdResolver
from .model import INITIATION_MESSAGE_TYPE, InitiationMessage
from .tool import InitiateConversationTool

__all__ = [
    "INITIATION_MESSAGE_TYPE",
    "InitiateConversationTool",
    "InitiationManager",
    "InitiationMessage",
    "InitiationSender",
    "SessionIdResolver",
]
