"""
Pluggable authorization base class for Conversation Thread Support.

Agent Kernel does not verify identity itself — the end user is assumed to
already have an authentication provider. Subclass Authoriser with the custom
logic needed to validate a Bearer token against that provider and resolve the
subject (user_id).
"""

from abc import ABC, abstractmethod
from typing import Optional


class Authoriser(ABC):
    """
    Base class for thread route authorization. The end user supplies a subclass
    and passes it to ThreadRESTRequestHandler. When no Authoriser is configured,
    thread routes remain open.
    """

    @abstractmethod
    def authorise(self, token: str) -> Optional[str]:
        """
        Validate a Bearer token against the caller's own authentication provider.

        A user_id string and None are the only valid returns — the thread router uses the
        returned value directly as the user_id filter, so any other type can never match a
        stored user_id and is rejected with a TypeError. Subclasses needing the
        full claims elsewhere should expose them through their own method and return only the
        subject from here.

        :param token: The Bearer token from the Authorization header.
        :return: The resolved subject (user_id) as a string when the token is valid, or None to reject.
        """
        pass
