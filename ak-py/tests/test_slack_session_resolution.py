"""
Inbound Slack session-id derivation for agent-initiated conversations: the
thread_ts lookup plus the DM fallback to the channel id (a DM reply can be
top-level or threaded, so only the DM channel id round-trips reliably).
"""

import pytest

from agentkernel.core.initiation import InitiationManager
from agentkernel.core.initiation.mapping.in_memory import InMemorySessionIdMappingStore


class FakeMappingTableCfg:
    table_name = "test-mapping"
    collection_name = "test-mapping"
    prefix = "ak:test-map:"
    ttl = 0


def make_fake_cfg(mapping_table=FakeMappingTableCfg):
    class FakeCfg:
        class session:
            type = "in_memory"
            cache = None

        class slack:
            agent = ""
            agent_acknowledgement = ""

        class api:
            max_file_size = 10 * 1024 * 1024

    FakeCfg.mapping_table = mapping_table
    return FakeCfg


@pytest.fixture(autouse=True)
def reset_state():
    InitiationManager.reset()
    InMemorySessionIdMappingStore().clear()
    yield
    InitiationManager.reset()
    InMemorySessionIdMappingStore().clear()


@pytest.fixture
def handler(monkeypatch):
    # AsyncApp only needs non-empty credentials at construction; nothing connects.
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test-token")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-signing-secret")
    monkeypatch.setattr("agentkernel.core.config.AKConfig.get", classmethod(lambda cls: make_fake_cfg()))
    from agentkernel.slack import AgentSlackRequestHandler

    return AgentSlackRequestHandler()


class TestSlackSessionDerivation:
    def test_channel_thread_mapping_resolves(self, handler):
        InitiationManager.get()._store.save("session-1", "1111.2222")
        assert handler._derive_session_id("1111.2222", "C42", None) == "session-1"

    def test_dm_top_level_reply_falls_back_to_channel_mapping(self, handler):
        # Initiated DM bound under the DM channel id; the reply's own ts misses.
        InitiationManager.get()._store.save("session-1", "D999")
        assert handler._derive_session_id("3333.4444", "D999", "im") == "session-1"

    def test_dm_threaded_reply_falls_back_to_channel_mapping(self, handler):
        # A reply threaded under the bot's DM message resolves the bot ts (miss) then the channel.
        InitiationManager.get()._store.save("session-1", "D999")
        assert handler._derive_session_id("1111.2222", "D999", "im") == "session-1"

    def test_dm_without_mapping_keeps_platform_derived_id(self, handler):
        # Reactive DM behavior unchanged: both lookups miss -> thread_ts.
        assert handler._derive_session_id("3333.4444", "D999", "im") == "3333.4444"

    def test_channel_message_never_uses_dm_fallback(self, handler):
        # A mapping under a channel id must not hijack non-DM messages.
        InitiationManager.get()._store.save("session-1", "C42")
        assert handler._derive_session_id("3333.4444", "C42", "channel") == "3333.4444"

    def test_thread_hit_wins_over_dm_fallback(self, handler):
        InitiationManager.get()._store.save("session-thread", "1111.2222")
        InitiationManager.get()._store.save("session-dm", "D999")
        assert handler._derive_session_id("1111.2222", "D999", "im") == "session-thread"

    def test_disabled_feature_is_identity(self, monkeypatch, handler):
        monkeypatch.setattr("agentkernel.core.config.AKConfig.get", classmethod(lambda cls: make_fake_cfg(mapping_table=None)))
        assert handler._derive_session_id("3333.4444", "D999", "im") == "3333.4444"
