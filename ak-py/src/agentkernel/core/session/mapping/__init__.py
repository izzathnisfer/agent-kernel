"""
Session ID Mapping stores for agent-initiated conversations.

Each store is paired with a session store backend, sharing its connection settings; the
namespace (table/collection name or key prefix) is derived from the session store's own and
the TTL is reused from it — see each backend module for the exact derivation. Session stores
build theirs in their constructor via :func:`build_mapping_store` and hand it out through
``SessionStore.get_mapping_store()``.
"""

import logging

from ...config import AKConfig
from ...util.factory import resolve_dotted
from ..base import MappingStore

_log = logging.getLogger("ak.core.session.mapping")


def build_mapping_store(default_factory: type[MappingStore]) -> MappingStore:
    """
    Builds the Session ID Mapping store for a session store backend.

    A ``session.initiation.store`` dotted path takes precedence, letting an operator bring
    their own MappingStore regardless of which session backend is in use; otherwise the
    backend's own paired store is constructed. Called from each session store's constructor,
    so a misconfigured dotted path surfaces while the session store is being built — at
    startup — rather than on the first request that needs a mapping.

    The block is read defensively because session stores are also constructed against
    minimal stand-in configs in tests; a config that does not expose the block has no
    bring-your-own override, which is the same outcome as leaving it unset.

    :param default_factory: The MappingStore subclass paired with the calling session store.
    :return: The bring-your-own store when configured, otherwise ``default_factory()``.
    :raises AKConfigError: If ``session.initiation.store`` is set but does not resolve to a
        MappingStore subclass.
    """
    session = getattr(AKConfig.get(), "session", None)
    store_path = getattr(getattr(session, "initiation", None), "store", None)
    if store_path:
        _log.info(f"Building session id mapping store from dotted path '{store_path}'")
        return resolve_dotted(store_path, base=MappingStore)()
    return default_factory()
