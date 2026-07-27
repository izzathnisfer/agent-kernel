from unittest.mock import MagicMock

import pytest

from agentkernel.core.initiation import InitiationManager, InitiationMessage, SessionIdResolver
from agentkernel.core.initiation.mapping import SessionIdMappingStoreBuilder
from agentkernel.core.initiation.mapping.base import SessionIdMappingStore
from agentkernel.core.initiation.mapping.dynamodb import PARTITION_KEY, VALUE_ATTRIBUTE, DynamoDBSessionIdMappingStore
from agentkernel.core.initiation.mapping.in_memory import InMemorySessionIdMappingStore
from agentkernel.core.initiation.mapping.redis import RedisSessionIdMappingStore
from agentkernel.core.util.factory import AKConfigError


@pytest.fixture(autouse=True)
def clear_in_memory_store():
    InMemorySessionIdMappingStore().clear()
    InitiationManager.reset()
    yield
    InMemorySessionIdMappingStore().clear()
    InitiationManager.reset()


_UNSET = object()


def make_fake_cfg(session_type: str, conversation_initiation_enabled: bool = True, initiation_store: str = None, redis=True, enabled=_UNSET):
    """
    Build a stand-in AKConfig.

    ``conversation_initiation_enabled`` is the effective gate (the real property's result),
    while ``enabled`` is the raw ``conversation_initiation.enabled`` field. They normally
    agree, so ``enabled`` defaults to mirroring the gate; pass ``enabled=None`` to model
    auto-enable (queue mode inferred the feature, the operator never set it), which
    ``InitiationManager.get()`` distinguishes from an explicit opt-in.
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

        class conversation_initiation:
            store = initiation_store

    FakeCfg.conversation_initiation.enabled = conversation_initiation_enabled if enabled is _UNSET else enabled
    FakeCfg.conversation_initiation_enabled = conversation_initiation_enabled
    if not redis:
        FakeCfg.session.redis = None
    return FakeCfg


class TestInMemoryStore:
    def test_save_and_lookup_both_directions(self):
        store = InMemorySessionIdMappingStore()
        store.save("session-1", "thread-1")
        assert store.get_session_id("thread-1") == "session-1"
        assert store.get_messaging_integration_thread_id("session-1") == "thread-1"

    def test_miss_returns_none(self):
        store = InMemorySessionIdMappingStore()
        assert store.get_session_id("unknown") is None
        assert store.get_messaging_integration_thread_id("unknown") is None

    def test_save_is_idempotent(self):
        store = InMemorySessionIdMappingStore()
        store.save("session-1", "thread-1")
        store.save("session-1", "thread-1")
        assert store.get_session_id("thread-1") == "session-1"

    def test_save_is_last_writer_wins(self):
        store = InMemorySessionIdMappingStore()
        store.save("session-1", "thread-1")
        store.save("session-2", "thread-1")
        assert store.get_session_id("thread-1") == "session-2"

    def test_shared_across_instances(self):
        InMemorySessionIdMappingStore().save("session-1", "thread-1")
        assert InMemorySessionIdMappingStore().get_session_id("thread-1") == "session-1"

    def test_clear(self):
        store = InMemorySessionIdMappingStore()
        store.save("session-1", "thread-1")
        store.clear()
        assert store.get_session_id("thread-1") is None
        assert store.get_messaging_integration_thread_id("session-1") is None


class TestRecordKeys:
    def test_directions_never_collide(self):
        # A session id equal to a thread id must still produce distinct records.
        assert SessionIdMappingStore.thread_record_key("x") != SessionIdMappingStore.session_record_key("x")


class TestBuilder:
    def test_defaults_to_in_memory(self, monkeypatch):
        monkeypatch.setattr("agentkernel.core.config.AKConfig.get", classmethod(lambda cls: make_fake_cfg("in_memory")))
        assert isinstance(SessionIdMappingStoreBuilder.build(), InMemorySessionIdMappingStore)

    def test_unknown_type_raises_config_error(self, monkeypatch):
        monkeypatch.setattr("agentkernel.core.config.AKConfig.get", classmethod(lambda cls: make_fake_cfg("not-a-backend")))
        with pytest.raises(AKConfigError, match="unknown session store type"):
            SessionIdMappingStoreBuilder.build()

    def test_follows_session_type_redis(self, monkeypatch):
        monkeypatch.setattr("agentkernel.core.config.AKConfig.get", classmethod(lambda cls: make_fake_cfg("redis")))
        store = SessionIdMappingStoreBuilder.build()
        assert isinstance(store, RedisSessionIdMappingStore)

    def test_redis_store_derives_prefix_and_reuses_session_ttl(self, monkeypatch):
        # url comes from session.redis; the mapping prefix suffixes the session prefix and TTL is reused.
        monkeypatch.setattr("agentkernel.core.config.AKConfig.get", classmethod(lambda cls: make_fake_cfg("redis")))
        store = SessionIdMappingStoreBuilder.build()
        assert store._driver._url == "redis://example:6379"
        assert store._driver._prefix == "ak:sessions:id-mapping:"
        assert store._driver.ttl == 60

    def test_redis_store_requires_session_redis_block(self, monkeypatch):
        monkeypatch.setattr("agentkernel.core.config.AKConfig.get", classmethod(lambda cls: make_fake_cfg("redis", redis=False)))
        with pytest.raises(ValueError, match="session.redis"):
            SessionIdMappingStoreBuilder.build()


class TestRedisStoreOperations:
    """Data operations against a mocked driver (no live Redis required)."""

    @pytest.fixture
    def store(self, monkeypatch):
        monkeypatch.setattr("agentkernel.core.config.AKConfig.get", classmethod(lambda cls: make_fake_cfg("redis")))
        store = RedisSessionIdMappingStore()
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
        store = DynamoDBSessionIdMappingStore()
        store._driver = MagicMock()
        return store

    def test_table_name_derived_from_session_table(self, monkeypatch):
        monkeypatch.setattr("agentkernel.core.config.AKConfig.get", classmethod(lambda cls: make_fake_cfg("dynamodb")))
        store = DynamoDBSessionIdMappingStore()
        assert store._driver._table_name == "ak-sessions-id-mapping"

    def test_save_puts_both_items(self, store):
        store.save("session-1", "thread-1")
        put_items = [call.args[0] for call in store._driver.put.call_args_list]
        assert {PARTITION_KEY: "thread#thread-1", VALUE_ATTRIBUTE: "session-1"} in put_items
        assert {PARTITION_KEY: "session#session-1", VALUE_ATTRIBUTE: "thread-1"} in put_items

    def test_get_extracts_value_attribute(self, store):
        store._driver.get.return_value = {PARTITION_KEY: "thread#thread-1", VALUE_ATTRIBUTE: "session-1"}
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


class TestUnbuildableMappingStore:
    """
    A mapping store that cannot be built is handled by intent: auto-enable (queue mode
    inferred the feature) degrades to feature-off, while an explicit opt-in still raises.

    The trigger here is a ``session.type`` the builder cannot resolve — legal since the
    BYO-stores change, where ``session.type`` may be a dotted path to a SessionStore that
    has no mapping-store counterpart.
    """

    def _cfg(self, monkeypatch, enabled):
        monkeypatch.setattr(
            "agentkernel.core.config.AKConfig.get",
            classmethod(lambda cls: make_fake_cfg("my_pkg.my_module.MySessionStore", enabled=enabled)),
        )

    def test_auto_enabled_degrades_to_feature_off(self, monkeypatch, caplog):
        self._cfg(monkeypatch, enabled=None)

        with caplog.at_level("WARNING"):
            assert InitiationManager.get() is None

        assert "conversation_initiation.store" in caplog.text

    def test_auto_enabled_keeps_inbound_messages_working(self, monkeypatch):
        """The point of degrading: resolution falls back instead of failing every request."""
        self._cfg(monkeypatch, enabled=None)

        assert SessionIdResolver().resolve_session_id("thread-1") == "thread-1"

    def test_auto_disabled_decision_is_cached(self, monkeypatch, caplog):
        self._cfg(monkeypatch, enabled=None)
        build = MagicMock(side_effect=AKConfigError("unknown session store type"))
        monkeypatch.setattr(SessionIdMappingStoreBuilder, "build", staticmethod(build))

        with caplog.at_level("WARNING"):
            assert InitiationManager.get() is None
            assert InitiationManager.get() is None

        assert build.call_count == 1
        assert caplog.text.count("conversation_initiation.store") == 1

    def test_explicitly_enabled_still_raises(self, monkeypatch):
        self._cfg(monkeypatch, enabled=True)

        with pytest.raises(AKConfigError):
            InitiationManager.get()

    def test_explicit_failure_is_not_cached_as_disabled(self, monkeypatch):
        """An explicit opt-in must keep raising, not silently degrade on the second call."""
        self._cfg(monkeypatch, enabled=True)

        with pytest.raises(AKConfigError):
            InitiationManager.get()
        with pytest.raises(AKConfigError):
            InitiationManager.get()

    def test_reset_clears_the_auto_disabled_decision(self, monkeypatch):
        self._cfg(monkeypatch, enabled=None)
        assert InitiationManager.get() is None

        InitiationManager.reset()
        monkeypatch.setattr("agentkernel.core.config.AKConfig.get", classmethod(lambda cls: make_fake_cfg("in_memory")))
        assert InitiationManager.get() is not None


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
