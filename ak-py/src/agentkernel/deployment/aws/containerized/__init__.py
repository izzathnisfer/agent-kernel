"""
AWS containerized deployment entrypoints.

``AWSRestAPI``, ``AWSWebsocketAPI`` and ``ECSWebSocketSystemRequestHandler`` are exported lazily
via PEP 562. They live under ``core.api``, which imports FastAPI — available only in the optional
``api`` extra. Every AWS entrypoint reaches this package through ``agentkernel.aws``, including the
serverless Lambda handlers, which install ``agentkernel[aws,...]`` and serve no routes of their
own: an eager import here makes FastAPI mandatory for all of them and fails them at init.
``ecs_io_handler`` already defers these imports into the function that starts the server.
"""

from typing import TYPE_CHECKING, Any

from .akagentrunner import ECSAgentRunner
from .akoutputconsumer import ECSOutputConsumer
from .ecs_io_handler import ECSIOHandler

if TYPE_CHECKING:
    from .core.api import AWSRestAPI, AWSWebsocketAPI, ECSWebSocketSystemRequestHandler

_LAZY_API_EXPORTS = ("AWSRestAPI", "AWSWebsocketAPI", "ECSWebSocketSystemRequestHandler")

__all__ = ["ECSAgentRunner", "ECSIOHandler", "ECSOutputConsumer", *_LAZY_API_EXPORTS]


def __dir__() -> list[str]:
    """
    List the package's contents including the lazily exported API classes.
    :return: Sorted attribute names, adding the lazy exports to what is already imported.
    """
    return sorted(set(globals()) | set(__all__))


def __getattr__(name: str) -> Any:
    """
    Resolve the lazily exported API classes — see the module docstring.
    :param name: The attribute being looked up on the package.
    :return: The resolved attribute.
    :raises AttributeError: If the name is not exported by this package.
    """
    if name in _LAZY_API_EXPORTS:
        from .core import api

        return getattr(api, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
