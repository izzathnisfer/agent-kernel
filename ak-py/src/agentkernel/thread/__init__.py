"""
Conversation Thread Support for Agent Kernel.

This package provides:
- Thread / ThreadMessage / ThreadAttachment models
- ThreadStore storage abstraction with pluggable backends
- ConversationThreadManager service façade
- ThreadNamingStrategy overridable naming strategy for auto-created threads
- Authoriser pluggable base class for thread route authorization
- ThreadRESTRequestHandler exposing the thread read endpoints over REST
"""

from .authoriser import Authoriser
from .manager import ConversationThreadManager
from .model import MessagePage, Thread, ThreadAttachment, ThreadMessage, ThreadPage
from .naming import ThreadNamingStrategy
from .rest import ThreadRESTRequestHandler
from .store import ThreadStore, ThreadStoreBuilder
