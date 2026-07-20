"""
Inbound Slack session-id derivation for agent-initiated conversations.

DMs and channels are resolved identically: AgentSlackRequestHandler.handle()
calls the inherited resolve_session_id(thread_ts) directly, with no Slack- or
DM-specific fallback. A reply must be threaded to continue an initiated
conversation; an un-threaded reply's own ts never matches a bound mapping, so
it starts a new session rather than guessing which prior conversation (if any)
it's answering.
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
    def test_channel_threaded_reply_resolves(self, handler):
        InitiationManager.get()._store.save("session-1", "1111.2222")
        assert handler.resolve_session_id("1111.2222") == "session-1"

    def test_dm_threaded_reply_resolves(self, handler):
        # DMs bind their own message ts, exactly like channels — no channel-id fallback.
        InitiationManager.get()._store.save("session-1", "1111.2222")
        assert handler.resolve_session_id("1111.2222") == "session-1"

    def test_unthreaded_dm_reply_starts_new_session(self, handler):
        # No mapping for this reply's own ts: unambiguous by construction, not a guess.
        InitiationManager.get()._store.save("session-1", "1111.2222")
        assert handler.resolve_session_id("3333.4444") == "3333.4444"

    def test_dm_channel_id_is_never_consulted(self, handler):
        # Regression guard: a mapping saved under a channel id (not a message ts) must
        # never be reachable from resolve_session_id — there is no channel-id fallback.
        InitiationManager.get()._store.save("session-1", "D999")
        assert handler.resolve_session_id("3333.4444") == "3333.4444"

    def test_disabled_feature_is_identity(self, monkeypatch, handler):
        monkeypatch.setattr(
            "agentkernel.core.config.AKConfig.get",
            classmethod(lambda cls: make_fake_cfg(mapping_table=None)),
        )
        assert handler.resolve_session_id("3333.4444") == "3333.4444"
