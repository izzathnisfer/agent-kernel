import importlib
import sys
from unittest.mock import MagicMock

import pytest

from agentkernel.core.initiation import InitiationManager, InitiationMessage, SessionIdResolver
from agentkernel.core.session.base import MappingStore, SessionStore
from agentkernel.core.session.dynamodb import MAPPING_PARTITION_KEY, MAPPING_VALUE_ATTRIBUTE, DynamoDBMappingStore
from agentkernel.core.session.in_memory import InMemoryMappingStore, InMemorySessionStore
from agentkernel.core.session.redis import RedisMappingStore, RedisSessionStore


@pytest.fixture(autouse=True)
def clear_in_memory_store():
    InMemoryMappingStore().clear()
    InitiationManager.reset()
    yield
    InMemoryMappingStore().clear()
    InitiationManager.reset()


def make_fake_cfg(session_type: str, conversation_initiation_enabled: bool = True, initiation_store: str = None, redis=True):
    """
    Build a stand-in AKConfig whose ``session`` block carries the nested ``initiation``
    sub-block the real config now uses.
    """

    class FakeCfg:
        class session:
            type = session_type

            class redis:
                url = "redis://example:6379"
                ttl = 60
                prefix = "ak:sessions:"

            class dynamodb:
                table_name = "ak-sessions"
                ttl = 120

            class cosmosdb:
                connection_string = "AccountEndpoint=https://example;"
                table_name = "aksessions"
                ttl = 0

            class firestore:
                collection_name = "ak_sessions"
                project_id = None
                database_id = None
                ttl = 300

            cache = None
            valkey = None

            class initiation:
                enabled = conversation_initiation_enabled
                store = initiation_store

    FakeCfg.conversation_initiation_enabled = conversation_initiation_enabled
    if not redis:
        FakeCfg.session.redis = None
    return FakeCfg


class TestInMemoryStore:
    def test_save_and_lookup_both_directions(self):
        store = InMemoryMappingStore()
        store.save("session-1", "thread-1")
        assert store.get_session_id("thread-1") == "session-1"
        assert store.get_messaging_integration_thread_id("session-1") == "thread-1"

    def test_miss_returns_none(self):
        store = InMemoryMappingStore()
        assert store.get_session_id("unknown") is None
        assert store.get_messaging_integration_thread_id("unknown") is None

    def test_save_is_idempotent(self):
        store = InMemoryMappingStore()
        store.save("session-1", "thread-1")
        store.save("session-1", "thread-1")
        assert store.get_session_id("thread-1") == "session-1"

    def test_save_is_last_writer_wins(self):
        store = InMemoryMappingStore()
        store.save("session-1", "thread-1")
        store.save("session-2", "thread-1")
        assert store.get_session_id("thread-1") == "session-2"

    def test_shared_across_instances(self):
        InMemoryMappingStore().save("session-1", "thread-1")
        assert InMemoryMappingStore().get_session_id("thread-1") == "session-1"

    def test_clear(self):
        store = InMemoryMappingStore()
        store.save("session-1", "thread-1")
        store.clear()
        assert store.get_session_id("thread-1") is None
        assert store.get_messaging_integration_thread_id("session-1") is None


class TestRecordKeys:
    def test_directions_never_collide(self):
        # A session id equal to a thread id must still produce distinct records.
        assert MappingStore.thread_record_key("x") != MappingStore.session_record_key("x")


class TestSessionStoreProvenance:
    """
    The mapping store belongs to the session store: each backend pairs itself with the
    matching MappingStore, so the two always share one connection and one namespace.
    """

    def test_in_memory_session_store_pairs_in_memory_mapping(self, monkeypatch):
        monkeypatch.setattr("agentkernel.core.config.AKConfig.get", classmethod(lambda cls: make_fake_cfg("in_memory")))
        assert isinstance(InMemorySessionStore().get_mapping_store(), InMemoryMappingStore)

    def test_redis_session_store_pairs_redis_mapping(self, monkeypatch):
        monkeypatch.setattr("agentkernel.core.config.AKConfig.get", classmethod(lambda cls: make_fake_cfg("redis")))
        assert isinstance(RedisSessionStore().get_mapping_store(), RedisMappingStore)

    def test_a_session_store_without_a_mapping_store_cannot_be_instantiated(self):
        """
        The whole point of making get_mapping_store() abstract: adding a session backend
        without its mapping counterpart fails immediately, not once someone enables the
        feature. This is a deliberate breaking change for bring-your-own stores.
        """

        class StorelessSessionStore(SessionStore):
            def new(self, session_id): ...

            def load(self, session_id, strict=False): ...

            def store(self, session): ...

            def clear(self): ...

        with pytest.raises(TypeError, match="get_mapping_store"):
            StorelessSessionStore()

    def test_redis_mapping_derives_prefix_and_reuses_session_ttl(self, monkeypatch):
        monkeypatch.setattr("agentkernel.core.config.AKConfig.get", classmethod(lambda cls: make_fake_cfg("redis")))
        store = RedisSessionStore().get_mapping_store()
        assert store._driver._url == "redis://example:6379"
        assert store._driver._prefix == "ak:sessions:id-mapping:"
        assert store._driver.ttl == 60

    def test_redis_mapping_requires_session_redis_block(self, monkeypatch):
        monkeypatch.setattr("agentkernel.core.config.AKConfig.get", classmethod(lambda cls: make_fake_cfg("redis", redis=False)))
        with pytest.raises(ValueError, match="session.redis"):
            RedisMappingStore()

    def test_byo_dotted_path_overrides_the_paired_backend(self, monkeypatch):
        """session.initiation.store wins even when the session backend has its own pairing."""
        path = "agentkernel.core.session.in_memory.InMemoryMappingStore"
        monkeypatch.setattr("agentkernel.core.config.AKConfig.get", classmethod(lambda cls: make_fake_cfg("redis", initiation_store=path)))
        assert isinstance(RedisSessionStore().get_mapping_store(), InMemoryMappingStore)


class TestRedisStoreOperations:
    """Data operations against a mocked driver (no live Redis required)."""

    @pytest.fixture
    def store(self, monkeypatch):
        monkeypatch.setattr("agentkernel.core.config.AKConfig.get", classmethod(lambda cls: make_fake_cfg("redis")))
        store = RedisMappingStore()
        store._driver = MagicMock()
        store._driver.key.side_effect = lambda suffix: f"ak:sessions:id-mapping:{suffix}"
        return store

    def test_save_writes_both_records(self, store):
        store.save("session-1", "thread-1")
        set_calls = {call.args for call in store._driver.set.call_args_list}
        assert set_calls == {
            ("ak:sessions:id-mapping:thread#thread-1", "session-1"),
            ("ak:sessions:id-mapping:session#session-1", "thread-1"),
        }

    def test_lookups_read_the_right_records(self, store):
        store._driver.get.return_value = "session-1"
        assert store.get_session_id("thread-1") == "session-1"
        store._driver.get.assert_called_with("ak:sessions:id-mapping:thread#thread-1")

        store._driver.get.return_value = "thread-1"
        assert store.get_messaging_integration_thread_id("session-1") == "thread-1"
        store._driver.get.assert_called_with("ak:sessions:id-mapping:session#session-1")


class TestDynamoDBStoreOperations:
    """Data operations against a mocked driver (no live table required)."""

    @pytest.fixture
    def store(self, monkeypatch):
        monkeypatch.setattr("agentkernel.core.config.AKConfig.get", classmethod(lambda cls: make_fake_cfg("dynamodb")))
        store = DynamoDBMappingStore()
        store._driver = MagicMock()
        return store

    def test_table_name_derived_from_session_table(self, monkeypatch):
        monkeypatch.setattr("agentkernel.core.config.AKConfig.get", classmethod(lambda cls: make_fake_cfg("dynamodb")))
        store = DynamoDBMappingStore()
        assert store._driver._table_name == "ak-sessions-id-mapping"

    def test_save_puts_both_items(self, store):
        store.save("session-1", "thread-1")
        put_items = [call.args[0] for call in store._driver.put.call_args_list]
        assert {MAPPING_PARTITION_KEY: "thread#thread-1", MAPPING_VALUE_ATTRIBUTE: "session-1"} in put_items
        assert {MAPPING_PARTITION_KEY: "session#session-1", MAPPING_VALUE_ATTRIBUTE: "thread-1"} in put_items

    def test_get_extracts_value_attribute(self, store):
        store._driver.get.return_value = {MAPPING_PARTITION_KEY: "thread#thread-1", MAPPING_VALUE_ATTRIBUTE: "session-1"}
        assert store.get_session_id("thread-1") == "session-1"
        store._driver.get.assert_called_with("thread#thread-1")

    def test_get_missing_item_returns_none(self, store):
        store._driver.get.return_value = None
        assert store.get_session_id("thread-1") is None


def make_initiation(session_id="session-1", user_id="monroe"):
    return InitiationMessage(
        session_id=session_id,
        message="Hi Monroe, your laptop is ready",
        target="U123",
        user_id=user_id,
        request_id="req-1",
    )


class TestInitiationManager:
    @pytest.fixture
    def enabled_cfg(self, monkeypatch):
        monkeypatch.setattr("agentkernel.core.config.AKConfig.get", classmethod(lambda cls: make_fake_cfg("in_memory")))

    def test_get_returns_none_when_disabled(self, monkeypatch):
        monkeypatch.setattr(
            "agentkernel.core.config.AKConfig.get", classmethod(lambda cls: make_fake_cfg("in_memory", conversation_initiation_enabled=False))
        )
        assert InitiationManager.get() is None

    def test_get_returns_shared_instance_when_enabled(self, enabled_cfg):
        manager = InitiationManager.get()
        assert manager is not None
        assert InitiationManager.get() is manager

    def test_resolve_hit_returns_mapped_session(self, enabled_cfg):
        manager = InitiationManager.get()
        manager._store.save("session-1", "thread-1")
        assert manager.resolve_session_id("thread-1") == "session-1"

    def test_resolve_miss_returns_id_unchanged(self, enabled_cfg):
        assert InitiationManager.get().resolve_session_id("thread-x") == "thread-x"

    def test_resolve_store_error_returns_id_unchanged(self, enabled_cfg):
        manager = InitiationManager.get()
        manager._store = MagicMock()
        manager._store.get_session_id.side_effect = RuntimeError("backend down")
        assert manager.resolve_session_id("thread-1") == "thread-1"

    def test_reverse_lookup_hit_miss_and_error(self, enabled_cfg):
        manager = InitiationManager.get()
        manager._store.save("session-1", "thread-1")
        assert manager.get_messaging_integration_thread_id("session-1") == "thread-1"
        assert manager.get_messaging_integration_thread_id("session-x") is None
        manager._store = MagicMock()
        manager._store.get_messaging_integration_thread_id.side_effect = RuntimeError("backend down")
        assert manager.get_messaging_integration_thread_id("session-1") is None

    def test_bind_saves_when_absent(self, enabled_cfg):
        manager = InitiationManager.get()
        manager.bind("session-1", "thread-1")
        assert manager._store.get_session_id("thread-1") == "session-1"

    def test_bind_skips_save_when_mapping_exists(self, enabled_cfg):
        manager = InitiationManager.get()
        manager._store.save("session-original", "thread-1")
        manager.bind("session-other", "thread-1")
        assert manager._store.get_session_id("thread-1") == "session-original"

    def test_complete_binds_mapping(self, enabled_cfg):
        manager = InitiationManager.get()
        manager.complete(make_initiation(), "thread-1")
        assert manager._store.get_session_id("thread-1") == "session-1"

    def test_complete_never_raises_on_store_failure(self, enabled_cfg):
        manager = InitiationManager.get()
        manager._store = MagicMock()
        manager._store.get_session_id.side_effect = RuntimeError("backend down")
        manager.complete(make_initiation(), "thread-1")  # must not raise

    def test_complete_initializes_thread_when_enabled(self, enabled_cfg, monkeypatch):
        thread_manager = MagicMock()

        class FakeThreadManager:
            @classmethod
            def get(cls):
                return thread_manager

        monkeypatch.setattr("agentkernel.core.initiation.manager.ConversationThreadManager", FakeThreadManager)
        InitiationManager.get().complete(make_initiation(), "thread-1")

        create_kwargs = thread_manager.get_or_create_thread.call_args.kwargs
        assert create_kwargs == {"session_id": "session-1", "user_id": "monroe", "first_prompt": "Hi Monroe, your laptop is ready"}
        thread_manager.append_message.assert_called_once_with("session-1", "assistant", "Hi Monroe, your laptop is ready")

    def test_complete_never_raises_on_thread_failure(self, enabled_cfg, monkeypatch):
        thread_manager = MagicMock()
        thread_manager.get_or_create_thread.side_effect = RuntimeError("thread store down")

        class FakeThreadManager:
            @classmethod
            def get(cls):
                return thread_manager

        monkeypatch.setattr("agentkernel.core.initiation.manager.ConversationThreadManager", FakeThreadManager)
        manager = InitiationManager.get()
        manager.complete(make_initiation(), "thread-1")  # must not raise
        # A thread-init failure must not lose the bind
        assert manager._store.get_session_id("thread-1") == "session-1"

    def test_dispatch_without_dispatcher_raises(self, enabled_cfg):
        with pytest.raises(ValueError, match="dispatcher"):
            InitiationManager.get().dispatch(make_initiation())

    def test_dispatch_invokes_registered_dispatcher(self, enabled_cfg):
        received = []
        InitiationManager.register_dispatcher(received.append)
        initiation = make_initiation()
        InitiationManager.get().dispatch(initiation)
        assert received == [initiation]


class TestSessionIdResolver:
    def test_identity_when_disabled(self, monkeypatch):
        monkeypatch.setattr(
            "agentkernel.core.config.AKConfig.get", classmethod(lambda cls: make_fake_cfg("in_memory", conversation_initiation_enabled=False))
        )
        assert SessionIdResolver().resolve_session_id("thread-1") == "thread-1"

    def test_delegates_to_manager_when_enabled(self, monkeypatch):
        monkeypatch.setattr("agentkernel.core.config.AKConfig.get", classmethod(lambda cls: make_fake_cfg("in_memory")))
        InitiationManager.get()._store.save("session-1", "thread-1")
        assert SessionIdResolver().resolve_session_id("thread-1") == "session-1"


class TestInitiationMessage:
    def test_defaults(self):
        initiation = make_initiation()
        assert initiation.type == "initiation"
        assert initiation.target_details is None


_REDIS_LIKE_MODULES = (
    "agentkernel.core.session.valkey",
    "agentkernel.core.session.redis",
    "agentkernel.core.util.driver.redis",
)


class TestExtraIsolation:
    """
    ``redis`` and ``valkey`` are separate optional extras, so the Valkey backend must import
    without the ``redis`` package installed. The shared ``_RedisLikeMappingStore`` therefore
    lives in ``session/redis_like.py`` (client-library-free) rather than in ``session/redis.py``,
    whose ``RedisDriver`` import pulls in ``redis``.

    Regression guard: an earlier revision had ``session/valkey.py`` import the shared base from
    ``session/redis.py``, which made ``agentkernel[valkey]`` unusable. The whole suite stayed
    green because the dev venv installs every extra — hence the explicit ``sys.modules`` block.
    """

    @pytest.fixture
    def redis_extra_missing(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "redis", None)  # simulate the redis extra not installed
        # delitem, not a raw sys.modules.pop: monkeypatch restores the original module objects on
        # teardown. A pop would leave the re-imported copies in place, and tests asserting class
        # identity against their own top-level import would then fail (tests/test_runtime.py:100).
        for name in _REDIS_LIKE_MODULES:
            monkeypatch.delitem(sys.modules, name, raising=False)

    def test_valkey_backend_imports_without_the_redis_extra(self, redis_extra_missing):
        module = importlib.import_module("agentkernel.core.session.valkey")

        assert module.ValkeySessionStore is not None
        # Nothing may pull the redis driver (and therefore `import redis`) back in transitively.
        assert "agentkernel.core.util.driver.redis" not in sys.modules

    def test_shared_base_is_the_same_class_for_both_backends(self):
        """The split must not fork the implementation — both backends share one base."""
        from agentkernel.core.session.redis import RedisMappingStore as RedisMapping
        from agentkernel.core.session.redis_like import _RedisLikeMappingStore
        from agentkernel.core.session.valkey import ValkeyMappingStore

        assert issubclass(RedisMapping, _RedisLikeMappingStore)
        assert issubclass(ValkeyMappingStore, _RedisLikeMappingStore)
