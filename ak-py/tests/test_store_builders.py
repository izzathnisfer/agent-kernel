"""Phase-2 tests: session / thread / multimodal storage factories on the shared pattern.

Covers the behaviour change (unknown type now fails loud instead of silently falling back to
an in-memory default) and the bring-your-own dotted-path hatch with each surface's
construction contract (session store gets ``cache=``, thread store is no-arg, attachment store
gets ``session_id``). The mapping store has no factory of its own — it comes from the session
store's abstract ``get_mapping_store()``, so a BYO mapping store arrives with a BYO session store.
"""

import types
from unittest.mock import Mock, patch

import pytest

from agentkernel.core.builder import SessionStoreBuilder
from agentkernel.core.config import AKConfig
from agentkernel.core.multimodal.storage.base import AttachmentStore
from agentkernel.core.multimodal.storage.in_memory import InMemoryAttachmentStore
from agentkernel.core.multimodal.storage.storage_manager import AttachmentStorageManager
from agentkernel.core.session.base import MappingStore, SessionStore
from agentkernel.core.session.in_memory import InMemoryMappingStore, InMemorySessionStore
from agentkernel.core.thread.store.base import ThreadStore, ThreadStoreBuilder
from agentkernel.core.thread.store.in_memory import InMemoryThreadStore
from agentkernel.core.util.factory import AKConfigError


def _patch_import(monkeypatch, module_name, namespace):
    """Make resolve_dotted's importlib return `namespace` for `module_name`."""
    import agentkernel.core.util.factory as fac

    real = fac.importlib.import_module
    monkeypatch.setattr(
        fac.importlib,
        "import_module",
        lambda name, *a, **k: namespace if name == module_name else real(name, *a, **k),
    )


# --- bring-your-own test doubles (each subclasses the real ABC) ------------- #


class _ByoSessionStore(SessionStore):
    def __init__(self, cache=None):
        self.cache = cache

    def new(self, session_id): ...

    def load(self, session_id, strict=False): ...

    def store(self, session): ...

    def clear(self): ...

    def get_mapping_store(self):
        return _ByoMappingStore()


class _ByoThreadStore(ThreadStore):
    def create(self, thread): ...

    def update_name(self, session_id, name): ...

    def load_metadata(self, session_id): ...

    def append_message(self, session_id, message): ...

    def get_messages(self, session_id, limit, offset=0): ...

    def list_threads(self, *args, **kwargs): ...

    def clear(self): ...


class _ByoAttachmentStore(AttachmentStore):
    def __init__(self, session_id):
        self.session_id = session_id

    def save(self, attachment, max_attachments): ...

    def get(self, attachment_id): ...

    def delete(self, attachment_id): ...


class _ByoMappingStore(MappingStore):
    def __init__(self):
        self.records = {}

    def get_session_id(self, messaging_integration_thread_id): ...

    def get_messaging_integration_thread_id(self, session_id): ...

    def save(self, session_id, messaging_integration_thread_id): ...

    def clear(self): ...


# --- SessionStoreBuilder ---------------------------------------------------- #


def test_session_builder_default_in_memory():
    with patch.object(AKConfig, "get") as mock_get:
        cfg = Mock()
        cfg.session.type = "in_memory"
        cfg.session.cache = None
        mock_get.return_value = cfg
        assert isinstance(SessionStoreBuilder.build(), InMemorySessionStore)


def test_session_builder_unknown_type_fails_loud():
    with patch.object(AKConfig, "get") as mock_get:
        cfg = Mock()
        cfg.session.type = "reids"  # typo -> no longer a silent fallback to in_memory
        cfg.session.cache = None
        mock_get.return_value = cfg
        with pytest.raises(AKConfigError):
            SessionStoreBuilder.build()


def test_session_builder_byo_dotted_path_gets_cache(monkeypatch):
    _patch_import(monkeypatch, "byo_pkg", types.SimpleNamespace(Store=_ByoSessionStore))
    with patch.object(AKConfig, "get") as mock_get:
        cfg = Mock()
        cfg.session.type = "byo_pkg.Store"
        cfg.session.cache = None
        mock_get.return_value = cfg
        store = SessionStoreBuilder.build()
    assert isinstance(store, _ByoSessionStore)
    assert store.cache is None  # builder passed cache= per the session contract


# --- ThreadStoreBuilder ----------------------------------------------------- #


def test_thread_builder_default_memory():
    with patch.object(AKConfig, "get") as mock_get:
        cfg = Mock()
        cfg.thread.type = "memory"
        mock_get.return_value = cfg
        assert isinstance(ThreadStoreBuilder.build(), InMemoryThreadStore)


def test_thread_builder_unknown_type_fails_loud():
    with patch.object(AKConfig, "get") as mock_get:
        cfg = Mock()
        cfg.thread.type = "bogus"
        mock_get.return_value = cfg
        with pytest.raises(AKConfigError):
            ThreadStoreBuilder.build()


def test_thread_builder_not_configured_raises_value_error():
    with patch.object(AKConfig, "get") as mock_get:
        cfg = Mock()
        cfg.thread = None
        mock_get.return_value = cfg
        with pytest.raises(ValueError):
            ThreadStoreBuilder.build()


def test_thread_builder_byo_dotted_path(monkeypatch):
    _patch_import(monkeypatch, "byo_pkg", types.SimpleNamespace(Store=_ByoThreadStore))
    with patch.object(AKConfig, "get") as mock_get:
        cfg = Mock()
        cfg.thread.type = "byo_pkg.Store"
        mock_get.return_value = cfg
        assert isinstance(ThreadStoreBuilder.build(), _ByoThreadStore)


# --- multimodal attachment storage ----------------------------------------- #


def test_multimodal_default_in_memory():
    with patch.object(AKConfig, "get") as mock_get:
        cfg = Mock()
        cfg.multimodal.storage_type = "in_memory"
        mock_get.return_value = cfg
        assert isinstance(AttachmentStorageManager._build_driver("sess-1"), InMemoryAttachmentStore)


def test_multimodal_unknown_type_fails_loud():
    with patch.object(AKConfig, "get") as mock_get:
        cfg = Mock()
        cfg.multimodal.storage_type = "reids"
        mock_get.return_value = cfg
        with pytest.raises(AKConfigError):
            AttachmentStorageManager._build_driver("sess-1")


def test_multimodal_byo_dotted_path_gets_session_id(monkeypatch):
    _patch_import(monkeypatch, "byo_pkg", types.SimpleNamespace(Store=_ByoAttachmentStore))
    with patch.object(AKConfig, "get") as mock_get:
        cfg = Mock()
        cfg.multimodal.storage_type = "byo_pkg.Store"
        mock_get.return_value = cfg
        store = AttachmentStorageManager._build_driver("sess-1")
    assert isinstance(store, _ByoAttachmentStore)
    assert store.session_id == "sess-1"  # builder passed session_id per the multimodal contract


# --- mapping store: supplied only by the session store ---------------------- #
#
# There is no mapping-store config key and no builder: a bring-your-own mapping store
# arrives with a bring-your-own session store, because get_mapping_store() is abstract.


def test_builtin_session_store_supplies_its_paired_mapping_store():
    with patch.object(AKConfig, "get") as mock_get:
        cfg = Mock()
        cfg.session.type = "in_memory"
        cfg.session.cache = None
        mock_get.return_value = cfg
        assert isinstance(SessionStoreBuilder.build().get_mapping_store(), InMemoryMappingStore)


def test_byo_session_store_brings_its_own_mapping_store(monkeypatch):
    """The only BYO route: the custom session store returns whatever MappingStore it wants."""
    _patch_import(monkeypatch, "byo_pkg", types.SimpleNamespace(Store=_ByoSessionStore))
    with patch.object(AKConfig, "get") as mock_get:
        cfg = Mock()
        cfg.session.type = "byo_pkg.Store"
        cfg.session.cache = None
        mock_get.return_value = cfg
        store = SessionStoreBuilder.build()
    assert isinstance(store, _ByoSessionStore)
    assert isinstance(store.get_mapping_store(), _ByoMappingStore)


class _StorelessSessionStore(SessionStore):
    """A bring-your-own session store whose author forgot the mapping store."""

    def __init__(self, cache=None):
        self.cache = cache

    def new(self, session_id): ...

    def load(self, session_id, strict=False): ...

    def store(self, session): ...

    def clear(self): ...


def test_session_builder_rejects_a_store_without_a_mapping_store(monkeypatch):
    """
    get_mapping_store() is abstract, so a session store that omits it cannot be
    instantiated at all — the builder surfaces that at startup, before any request. This
    replaces the earlier runtime gate and is a deliberate breaking change for BYO stores.
    """
    _patch_import(monkeypatch, "byo_sessions", types.SimpleNamespace(Store=_StorelessSessionStore))
    with patch.object(AKConfig, "get") as mock_get:
        cfg = Mock()
        cfg.session.type = "byo_sessions.Store"
        cfg.session.cache = None
        mock_get.return_value = cfg
        with pytest.raises(TypeError, match="get_mapping_store"):
            SessionStoreBuilder.build()
