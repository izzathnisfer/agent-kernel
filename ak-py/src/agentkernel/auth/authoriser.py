"""
Pluggable authorization base class for route-level identity resolution.

Agent Kernel does not verify identity itself — the end user is assumed to
already have an authentication provider. Subclass Authoriser with the custom
logic needed to validate a Bearer token against that provider and resolve the
subject (user_id).

This lives in the auth package rather than with any one feature because several
route layers need the same contract: Conversation Thread Support scopes thread
reads to the resolved subject, and the schedule routes establish the owner of a
scheduled task.
"""

from abc import ABC, abstractmethod
from typing import Optional


class Authoriser(ABC):
    """
    Base class for route authorization. The end user supplies a subclass and
    passes it to the handler protecting the routes. When no Authoriser is
    configured, thread routes remain open; the schedule routes require one,
    because every scheduled task must have an authenticated owner.
    """

    @abstractmethod
    def authorise(self, token: str) -> Optional[str]:
        """
        Validate a Bearer token against the caller's own authentication provider.
        :param token: The Bearer token from the Authorization header.
        :return: The resolved subject (user_id) when the token is valid, or None to reject.
        """
        pass
