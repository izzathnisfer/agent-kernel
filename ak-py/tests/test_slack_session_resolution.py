"""
Inbound Slack session-id derivation for agent-initiated conversations.

DMs and channels are resolved identically: AgentSlackRequestHandler.handle()
calls the inherited resolve_session_id(thread_ts) directly, with no Slack- or
DM-specific fallback. A reply must be threaded to continue an initiated
conversation; an un-threaded reply's own ts never matches a bound mapping, so
it starts a new session rather than guessing which prior conversation (if any)
it's answering.

The TestHandleSessionResolution class drives handle(body, say) end-to-end —
that's the actual regression guard: it fails if a channel-id (or any other)
fallback is ever reintroduced inside handle() itself. TestSlackSessionDerivation
below it exercises resolve_session_id directly as a lower-level supplement.
"""

import pytest

from agentkernel.core.initiation import InitiationManager
from agentkernel.core.initiation.mapping.in_memory import InMemorySessionIdMappingStore


def make_fake_cfg(conversation_initiation_enabled=True):
    class FakeCfg:
        class session:
            type = "in_memory"
            cache = None

        class slack:
            agent = ""
            agent_acknowledgement = ""

        class api:
            max_file_size = 10 * 1024 * 1024

        class conversation_initiation:
            enabled = conversation_initiation_enabled
            store = None

    FakeCfg.conversation_initiation_enabled = conversation_initiation_enabled
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

    h = AgentSlackRequestHandler()
    h._bot_id = "BOTID"  # skips the auth_test() network call in handle()
    return h


class FakeAgentService:
    """Records the session_id passed to select(); agent stays None so handle()
    takes its existing "no agent available" early-exit right after select(),
    without needing to mock file processing or run_multi."""

    def __init__(self):
        self.agent = None
        self.selected_session_id = None

    def select(self, session_id, name):
        self.selected_session_id = session_id


class FakeSay:
    def __init__(self):
        self.calls = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return {"ts": "9999.0000", "channel": kwargs.get("channel", "D999")}


def dm_body(ts: str, thread_ts: str | None = None) -> dict:
    body = {
        "user": "U777",
        "text": "hello",
        "channel": "D999",
        "channel_type": "im",
        "ts": ts,
    }
    if thread_ts is not None:
        body["thread_ts"] = thread_ts
    return body


class TestHandleSessionResolution:
    """Drives handle(body, say) end-to-end — the actual regression guard."""

    @pytest.mark.asyncio
    async def test_threaded_dm_reply_resolves_to_mapped_session(self, handler, monkeypatch):
        InitiationManager.get()._store.save("session-1", "1111.2222")
        fake_service = FakeAgentService()
        monkeypatch.setattr(
            "agentkernel.integration.slack.slack_chat.AgentService",
            lambda: fake_service,
        )

        await handler.handle(dm_body(ts="5555.6666", thread_ts="1111.2222"), FakeSay())

        assert fake_service.selected_session_id == "session-1"

    @pytest.mark.asyncio
    async def test_unthreaded_dm_reply_resolves_to_its_own_ts(self, handler, monkeypatch):
        InitiationManager.get()._store.save("session-1", "1111.2222")
        fake_service = FakeAgentService()
        monkeypatch.setattr(
            "agentkernel.integration.slack.slack_chat.AgentService",
            lambda: fake_service,
        )

        await handler.handle(dm_body(ts="3333.4444"), FakeSay())

        assert fake_service.selected_session_id == "3333.4444"

    @pytest.mark.asyncio
    async def test_channel_id_mapping_is_never_consulted_by_handle(self, handler, monkeypatch):
        # Regression guard: a mapping saved under the DM channel id (not a message
        # ts) must never be reachable from handle() — no channel-id fallback exists.
        InitiationManager.get()._store.save("session-dm", "D999")
        fake_service = FakeAgentService()
        monkeypatch.setattr(
            "agentkernel.integration.slack.slack_chat.AgentService",
            lambda: fake_service,
        )

        await handler.handle(dm_body(ts="3333.4444"), FakeSay())

        assert fake_service.selected_session_id == "3333.4444"


class TestSlackSessionDerivation:
    """Lower-level supplement: resolve_session_id itself (SessionIdResolver mixin)."""

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
        # A mapping saved under a channel id (not a message ts) must never be
        # reachable from resolve_session_id — there is no channel-id fallback.
        InitiationManager.get()._store.save("session-1", "D999")
        assert handler.resolve_session_id("3333.4444") == "3333.4444"

    def test_disabled_feature_is_identity(self, monkeypatch, handler):
        monkeypatch.setattr(
            "agentkernel.core.config.AKConfig.get",
            classmethod(lambda cls: make_fake_cfg(conversation_initiation_enabled=False)),
        )
        assert handler.resolve_session_id("3333.4444") == "3333.4444"
