"""
Public entrypoint for Agent Kernel's AWS support.

The star import below picks up only the names ``deployment.aws`` holds eagerly, which is what keeps
this module importable without FastAPI — the serverless Lambda handlers install
``agentkernel[aws,...]`` with no ``api`` extra. ``__getattr__`` covers the rest, so
``from agentkernel.aws import AWSWebsocketAPI`` still works and pulls FastAPI in only then.
"""

import importlib.metadata
from typing import Any

try:
    __version__ = importlib.metadata.version("agentkernel")
except importlib.metadata.PackageNotFoundError:
    __version__ = "0.1.0"

from .deployment.aws import *
from .deployment.aws import _LAZY_API_EXPORTS


def __dir__() -> list[str]:
    """
    List this module's contents including the lazily resolved AWS names.
    :return: Sorted attribute names.
    """
    from .deployment import aws

    return sorted(set(globals()) | set(dir(aws)))


def __getattr__(name: str) -> Any:
    """
    Delegate the FastAPI-backed API classes, which the star import deliberately leaves unbound, to
    ``agentkernel.deployment.aws``. Restricted to those names so that an ``__all__`` lookup on this
    module cannot resolve them eagerly.
    :param name: The attribute being looked up on the module.
    :return: The resolved attribute.
    :raises AttributeError: If the name is not one of the lazily exported API classes.
    """
    if name in _LAZY_API_EXPORTS:
        from .deployment import aws

        return getattr(aws, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
