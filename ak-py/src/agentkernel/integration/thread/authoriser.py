"""
Conversation Thread Support authorization.

The Authoriser contract is shared with the other route layers that resolve a
Bearer token to a subject, so it lives in the auth package. It is re-exported
here to keep ``agentkernel.thread.Authoriser`` and
``agentkernel.integration.thread.Authoriser`` working as documented.
"""

from ...auth.authoriser import Authoriser

__all__ = ["Authoriser"]
