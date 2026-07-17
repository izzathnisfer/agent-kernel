"""
Agent-initiated conversations (AK-134).

Provides the Session ID Mapping store that associates the session created by
the Agent Runner with the messaging platform's thread identifier, the
InitiationManager façade that resolves/binds those mappings and dispatches
initiation messages, and the InitiationSender / SessionIdResolver contracts for
the handler roles.
"""

from .manager import InitiationManager, InitiationSender, SessionIdResolver
from .mapping import SessionIdMappingStoreBuilder
from .mapping.base import SessionIdMappingStore
from .model import INITIATION_MESSAGE_TYPE, InitiationMessage
from .tools import InitiateConversationTool

__all__ = [
    "INITIATION_MESSAGE_TYPE",
    "InitiateConversationTool",
    "InitiationManager",
    "InitiationMessage",
    "InitiationSender",
    "SessionIdMappingStore",
    "SessionIdMappingStoreBuilder",
    "SessionIdResolver",
]
