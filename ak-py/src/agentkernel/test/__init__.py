import importlib.metadata

try:
    __version__ = importlib.metadata.version("agentkernel")
except importlib.metadata.PackageNotFoundError:
    __version__ = "0.1.0"

from .config import AKTestConfig
from .core.clients.cli import CLIClient
from .core.model import Mode
from .test import Test
