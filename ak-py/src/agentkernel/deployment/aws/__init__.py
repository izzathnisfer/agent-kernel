"""
AWS deployment entrypoints, serverless and containerized.

The FastAPI-backed API classes stay lazy all the way up this re-export chain — see
``containerized/__init__.py`` for why. Deliberately no ``__all__``: ``agentkernel/aws.py``
star-imports this module, and ``import *`` resolves every name in ``__all__`` eagerly, which would
undo the laziness and pull FastAPI back into the serverless Lambdas.
"""

import importlib.metadata
from typing import TYPE_CHECKING, Any

try:
    __version__ = importlib.metadata.version("agentkernel")
except importlib.metadata.PackageNotFoundError:
    __version__ = "0.1.0"

from .containerized import _LAZY_API_EXPORTS, ECSAgentRunner, ECSIOHandler, ECSOutputConsumer
from .containerized.core import ECSSQSConsumer
from .core.sqs_handler import SQSHandler
from .serverless import APIGatewayAuthorizer, Lambda, ResponseHandler, ServerlessAgentRunner, WebsocketConnectionHandler
from .serverless.core import LambdaSQSConsumer

if TYPE_CHECKING:
    from .containerized import AWSRestAPI, AWSWebsocketAPI, ECSWebSocketSystemRequestHandler


def __dir__() -> list[str]:
    """
    List this package's contents including the names the containerized package resolves lazily.
    :return: Sorted attribute names.
    """
    from . import containerized

    return sorted(set(globals()) | set(dir(containerized)))


def __getattr__(name: str) -> Any:
    """
    Delegate the FastAPI-backed API classes to the containerized package, which resolves them on
    demand. Restricted to those names on purpose: a blanket delegation also answers the
    ``__all__`` lookup that ``import *`` performs, handing back the containerized package's list
    and resolving every lazy name eagerly.
    :param name: The attribute being looked up on the package.
    :return: The resolved attribute.
    :raises AttributeError: If the name is not one of the lazily exported API classes.
    """
    if name in _LAZY_API_EXPORTS:
        from . import containerized

        return getattr(containerized, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
