"""
Conversation Thread Support for Agent Kernel.

This package provides:
- Thread / ThreadMessage / ThreadAttachment models
- ThreadStore storage abstraction with pluggable backends
- ConversationThreadManager service façade
- ThreadNamingStrategy overridable naming strategy for auto-created threads
- Authoriser pluggable base class for thread route authorization
- ThreadRESTRequestHandler exposing the thread read endpoints over REST

``ThreadRESTRequestHandler`` is exported lazily via PEP 562. It lives in ``rest.py``, which
imports FastAPI — available only in the optional ``api`` extra. Threads themselves need no web
framework: a serverless deployment stores, names and reads threads without ever serving a route.
Importing it eagerly here would make the ``api`` extra a hard requirement of thread support and
break those deployments on their first chat request. Only asking for the name pulls FastAPI in.
"""

from typing import TYPE_CHECKING, Any

from .authoriser import Authoriser
from .manager import ConversationThreadManager
from .model import MessagePage, Thread, ThreadAttachment, ThreadMessage, ThreadPage
from .naming import ThreadNamingStrategy
from .store import ThreadStore, ThreadStoreBuilder

if TYPE_CHECKING:
    from .rest import ThreadRESTRequestHandler


def __getattr__(name: str) -> Any:
    """
    Resolve this package's lazily exported names — see the module docstring.
    :param name: The attribute being looked up on the package.
    :return: The resolved attribute.
    :raises AttributeError: If the name is not exported by this package.
    """
    if name == "ThreadRESTRequestHandler":
        from .rest import ThreadRESTRequestHandler

        return ThreadRESTRequestHandler
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
