"""
Inbound session-id derivation for agent-initiated conversations, for the
messaging integrations not covered by test_slack_session_resolution.py:
WhatsApp, Telegram, Messenger, Instagram, and Gmail (Teams is the one
integration left untested — see docs/specs/ak-134/plan.md's "Azure exception").

Each handler mixes in SessionIdResolver (directly, or via RESTRequestHandler)
and resolves the platform's own inbound identifier — phone number, chat id,
sender PSID/IGSID, Gmail thread id — through the Session ID Mapping before
falling back to the platform-derived id unchanged. These tests drive each
handler's own inbound-message method end to end (not resolve_session_id in
isolation), mirroring test_slack_session_resolution.py's TestHandleSessionResolution.
"""

from types import SimpleNamespace

import pytest

from agentkernel.core.initiation import InitiationManager
from agentkernel.core.session.in_memory import InMemoryMappingStore


@pytest.fixture(autouse=True)
def reset_state():
    InitiationManager.reset()
    InMemoryMappingStore().clear()
    yield
    InitiationManager.reset()
    InMemoryMappingStore().clear()


class FakeAgentService:
    """Records the session_id passed to select(); agent stays None so the
    handler takes its existing "no agent available" early-exit right after
    select(), without needing to mock a real agent run."""

    def __init__(self):
        self.agent = None
        self.selected_session_id = None

    def select(self, session_id, name=None, **kwargs):
        self.selected_session_id = session_id


async def _noop_async(*args, **kwargs):
    return None


# --------------------------------------------------------------------------- #
# WhatsApp
# --------------------------------------------------------------------------- #


def make_whatsapp_cfg(conversation_initiation_enabled=True):
    class FakeCfg:
        class session:
            type = "in_memory"
            cache = None

        class whatsapp:
            agent = ""
            agent_acknowledgement = ""
            verify_token = "test-verify-token"
            access_token = "test-access-token"
            app_secret = ""
            phone_number_id = "test-phone-id"
            api_version = "v24.0"

        class api:
            max_file_size = 10 * 1024 * 1024

    FakeCfg.conversation_initiation_enabled = conversation_initiation_enabled
    FakeCfg.session.initiation = SimpleNamespace(enabled=conversation_initiation_enabled, store=None)
    return FakeCfg


@pytest.fixture
def whatsapp_handler(monkeypatch):
    monkeypatch.setattr("agentkernel.core.config.AKConfig.get", classmethod(lambda cls: make_whatsapp_cfg()))
    from agentkernel.whatsapp import AgentWhatsAppRequestHandler

    handler = AgentWhatsAppRequestHandler()
    monkeypatch.setattr(handler, "_send_message", _noop_async)
    return handler


def whatsapp_message(message_id: str, from_number: str, text: str = "hello") -> dict:
    return {"id": message_id, "from": from_number, "type": "text", "text": {"body": text}}


class TestWhatsAppSessionResolution:
    @pytest.mark.asyncio
    async def test_mapped_thread_resolves_to_session(self, whatsapp_handler, monkeypatch):
        InitiationManager.get()._store.save("session-1", "15551234567")
        fake_service = FakeAgentService()
        monkeypatch.setattr("agentkernel.integration.whatsapp.whatsapp_chat.AgentService", lambda: fake_service)

        await whatsapp_handler._handle_message(whatsapp_message("wamid.1", "15551234567"), {})

        assert fake_service.selected_session_id == "session-1"

    @pytest.mark.asyncio
    async def test_unmapped_number_resolves_to_itself(self, whatsapp_handler, monkeypatch):
        fake_service = FakeAgentService()
        monkeypatch.setattr("agentkernel.integration.whatsapp.whatsapp_chat.AgentService", lambda: fake_service)

        await whatsapp_handler._handle_message(whatsapp_message("wamid.1", "15559999999"), {})

        assert fake_service.selected_session_id == "15559999999"

    @pytest.mark.asyncio
    async def test_disabled_feature_is_identity(self, whatsapp_handler, monkeypatch):
        monkeypatch.setattr("agentkernel.core.config.AKConfig.get", classmethod(lambda cls: make_whatsapp_cfg(conversation_initiation_enabled=False)))
        InitiationManager.get()  # no-op: feature disabled, manager stays None
        fake_service = FakeAgentService()
        monkeypatch.setattr("agentkernel.integration.whatsapp.whatsapp_chat.AgentService", lambda: fake_service)

        await whatsapp_handler._handle_message(whatsapp_message("wamid.1", "15551234567"), {})

        assert fake_service.selected_session_id == "15551234567"


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #


def make_telegram_cfg(conversation_initiation_enabled=True):
    class FakeCfg:
        class session:
            type = "in_memory"
            cache = None

        class telegram:
            agent = ""
            bot_token = "test-bot-token"
            webhook_secret = ""
            api_version = "bot"

        class api:
            max_file_size = 10 * 1024 * 1024

    FakeCfg.conversation_initiation_enabled = conversation_initiation_enabled
    FakeCfg.session.initiation = SimpleNamespace(enabled=conversation_initiation_enabled, store=None)
    return FakeCfg


@pytest.fixture
def telegram_handler(monkeypatch):
    monkeypatch.setattr("agentkernel.core.config.AKConfig.get", classmethod(lambda cls: make_telegram_cfg()))
    from agentkernel.telegram import AgentTelegramRequestHandler

    handler = AgentTelegramRequestHandler()
    monkeypatch.setattr(handler, "_send_message", _noop_async)
    monkeypatch.setattr(handler, "_send_chat_action", _noop_async)
    return handler


def telegram_body(chat_id: int, message_id: int, text: str = "hello") -> dict:
    return {"message": {"message_id": message_id, "chat": {"id": chat_id}, "text": text}}


class TestTelegramSessionResolution:
    @pytest.mark.asyncio
    async def test_mapped_chat_resolves_to_session(self, telegram_handler, monkeypatch):
        InitiationManager.get()._store.save("session-1", "555555")
        fake_service = FakeAgentService()
        monkeypatch.setattr("agentkernel.integration.telegram.telegram_chat.AgentService", lambda: fake_service)

        await telegram_handler._process_webhook_body(telegram_body(555555, 1001))

        assert fake_service.selected_session_id == "session-1"

    @pytest.mark.asyncio
    async def test_unmapped_chat_resolves_to_itself(self, telegram_handler, monkeypatch):
        fake_service = FakeAgentService()
        monkeypatch.setattr("agentkernel.integration.telegram.telegram_chat.AgentService", lambda: fake_service)

        await telegram_handler._process_webhook_body(telegram_body(999999, 1002))

        assert fake_service.selected_session_id == "999999"

    @pytest.mark.asyncio
    async def test_disabled_feature_is_identity(self, telegram_handler, monkeypatch):
        monkeypatch.setattr("agentkernel.core.config.AKConfig.get", classmethod(lambda cls: make_telegram_cfg(conversation_initiation_enabled=False)))
        fake_service = FakeAgentService()
        monkeypatch.setattr("agentkernel.integration.telegram.telegram_chat.AgentService", lambda: fake_service)

        await telegram_handler._process_webhook_body(telegram_body(555555, 1003))

        assert fake_service.selected_session_id == "555555"


# --------------------------------------------------------------------------- #
# Messenger
# --------------------------------------------------------------------------- #


def make_messenger_cfg(conversation_initiation_enabled=True):
    class FakeCfg:
        class session:
            type = "in_memory"
            cache = None

        class messenger:
            agent = ""
            verify_token = "test-verify-token"
            access_token = "test-access-token"
            app_secret = ""
            api_version = "v24.0"

        class api:
            max_file_size = 10 * 1024 * 1024

    FakeCfg.conversation_initiation_enabled = conversation_initiation_enabled
    FakeCfg.session.initiation = SimpleNamespace(enabled=conversation_initiation_enabled, store=None)
    return FakeCfg


@pytest.fixture
def messenger_handler(monkeypatch):
    monkeypatch.setattr("agentkernel.core.config.AKConfig.get", classmethod(lambda cls: make_messenger_cfg()))
    from agentkernel.messenger import AgentMessengerRequestHandler

    handler = AgentMessengerRequestHandler()
    monkeypatch.setattr(handler, "_mark_seen", _noop_async)
    monkeypatch.setattr(handler, "_send_typing_indicator", _noop_async)
    monkeypatch.setattr(handler, "_send_message", _noop_async)
    return handler


def messenger_event(sender_id: str, mid: str, text: str = "hello") -> dict:
    return {"sender": {"id": sender_id}, "message": {"mid": mid, "text": text}}


class TestMessengerSessionResolution:
    @pytest.mark.asyncio
    async def test_mapped_sender_resolves_to_session(self, messenger_handler, monkeypatch):
        InitiationManager.get()._store.save("session-1", "PSID12345")
        fake_service = FakeAgentService()
        monkeypatch.setattr("agentkernel.integration.messenger.messenger_chat.AgentService", lambda: fake_service)

        await messenger_handler._handle_message(messenger_event("PSID12345", "mid.1"))

        assert fake_service.selected_session_id == "session-1"

    @pytest.mark.asyncio
    async def test_unmapped_sender_resolves_to_itself(self, messenger_handler, monkeypatch):
        fake_service = FakeAgentService()
        monkeypatch.setattr("agentkernel.integration.messenger.messenger_chat.AgentService", lambda: fake_service)

        await messenger_handler._handle_message(messenger_event("PSID99999", "mid.2"))

        assert fake_service.selected_session_id == "PSID99999"

    @pytest.mark.asyncio
    async def test_disabled_feature_is_identity(self, messenger_handler, monkeypatch):
        monkeypatch.setattr(
            "agentkernel.core.config.AKConfig.get", classmethod(lambda cls: make_messenger_cfg(conversation_initiation_enabled=False))
        )
        fake_service = FakeAgentService()
        monkeypatch.setattr("agentkernel.integration.messenger.messenger_chat.AgentService", lambda: fake_service)

        await messenger_handler._handle_message(messenger_event("PSID12345", "mid.3"))

        assert fake_service.selected_session_id == "PSID12345"


# --------------------------------------------------------------------------- #
# Instagram
# --------------------------------------------------------------------------- #


def make_instagram_cfg(conversation_initiation_enabled=True):
    class FakeCfg:
        class session:
            type = "in_memory"
            cache = None

        class instagram:
            agent = ""
            verify_token = "test-verify-token"
            access_token = "test-access-token"
            app_secret = ""
            instagram_account_id = ""
            api_version = "v21.0"

        class api:
            max_file_size = 10 * 1024 * 1024

    FakeCfg.conversation_initiation_enabled = conversation_initiation_enabled
    FakeCfg.session.initiation = SimpleNamespace(enabled=conversation_initiation_enabled, store=None)
    return FakeCfg


@pytest.fixture
def instagram_handler(monkeypatch):
    monkeypatch.setattr("agentkernel.core.config.AKConfig.get", classmethod(lambda cls: make_instagram_cfg()))
    from agentkernel.instagram import AgentInstagramRequestHandler

    handler = AgentInstagramRequestHandler()
    monkeypatch.setattr(handler, "_mark_seen", _noop_async)
    monkeypatch.setattr(handler, "_send_typing_indicator", _noop_async)
    monkeypatch.setattr(handler, "_send_message", _noop_async)
    return handler


def instagram_event(sender_id: str, mid: str, text: str = "hello") -> dict:
    return {"sender": {"id": sender_id}, "message": {"mid": mid, "text": text}}


class TestInstagramSessionResolution:
    @pytest.mark.asyncio
    async def test_mapped_sender_resolves_to_session(self, instagram_handler, monkeypatch):
        InitiationManager.get()._store.save("session-1", "IGSID12345")
        fake_service = FakeAgentService()
        monkeypatch.setattr("agentkernel.integration.instagram.instagram_chat.AgentService", lambda: fake_service)

        await instagram_handler._handle_message(instagram_event("IGSID12345", "mid.1"))

        assert fake_service.selected_session_id == "session-1"

    @pytest.mark.asyncio
    async def test_unmapped_sender_resolves_to_itself(self, instagram_handler, monkeypatch):
        fake_service = FakeAgentService()
        monkeypatch.setattr("agentkernel.integration.instagram.instagram_chat.AgentService", lambda: fake_service)

        await instagram_handler._handle_message(instagram_event("IGSID99999", "mid.2"))

        assert fake_service.selected_session_id == "IGSID99999"

    @pytest.mark.asyncio
    async def test_disabled_feature_is_identity(self, instagram_handler, monkeypatch):
        monkeypatch.setattr(
            "agentkernel.core.config.AKConfig.get", classmethod(lambda cls: make_instagram_cfg(conversation_initiation_enabled=False))
        )
        fake_service = FakeAgentService()
        monkeypatch.setattr("agentkernel.integration.instagram.instagram_chat.AgentService", lambda: fake_service)

        await instagram_handler._handle_message(instagram_event("IGSID12345", "mid.3"))

        assert fake_service.selected_session_id == "IGSID12345"


# --------------------------------------------------------------------------- #
# Gmail — two resolution points (gmail_chat.py:267 and :413)
# --------------------------------------------------------------------------- #


def make_gmail_cfg(conversation_initiation_enabled=True):
    class FakeCfg:
        class session:
            type = "in_memory"
            cache = None

        class gmail:
            agent = ""
            token_file = "token.pickle"
            poll_interval = 30
            label_filter = "INBOX"

    FakeCfg.conversation_initiation_enabled = conversation_initiation_enabled
    FakeCfg.session.initiation = SimpleNamespace(enabled=conversation_initiation_enabled, store=None)
    return FakeCfg


@pytest.fixture
def gmail_handler(monkeypatch):
    monkeypatch.setenv("AK_GMAIL__CLIENT_ID", "test-client-id")
    monkeypatch.setenv("AK_GMAIL__CLIENT_SECRET", "test-client-secret")
    monkeypatch.setattr("agentkernel.core.config.AKConfig.get", classmethod(lambda cls: make_gmail_cfg()))
    from agentkernel.gmail import AgentGmailRequestHandler

    return AgentGmailRequestHandler()


class TestGmailProcessWithAgentResolution:
    """Site B (gmail_chat.py:413) in isolation: no Gmail API/service faking needed."""

    @pytest.mark.asyncio
    async def test_mapped_session_id_resolves(self, gmail_handler, monkeypatch):
        InitiationManager.get()._store.save("session-1", "thread-abc")
        fake_service = FakeAgentService()
        monkeypatch.setattr("agentkernel.integration.gmail.gmail_chat.AgentService", lambda: fake_service)

        await gmail_handler._process_with_agent(sender="someone@example.com", subject="Hi", body="hello", session_id="thread-abc")

        assert fake_service.selected_session_id == "session-1"

    @pytest.mark.asyncio
    async def test_unmapped_session_id_resolves_to_itself(self, gmail_handler, monkeypatch):
        fake_service = FakeAgentService()
        monkeypatch.setattr("agentkernel.integration.gmail.gmail_chat.AgentService", lambda: fake_service)

        await gmail_handler._process_with_agent(sender="someone@example.com", subject="Hi", body="hello", session_id="thread-xyz")

        assert fake_service.selected_session_id == "thread-xyz"

    @pytest.mark.asyncio
    async def test_missing_session_id_falls_back_to_sender(self, gmail_handler, monkeypatch):
        fake_service = FakeAgentService()
        monkeypatch.setattr("agentkernel.integration.gmail.gmail_chat.AgentService", lambda: fake_service)

        await gmail_handler._process_with_agent(sender="someone@example.com", subject="Hi", body="hello", session_id=None)

        assert fake_service.selected_session_id == "someone@example.com"

    @pytest.mark.asyncio
    async def test_disabled_feature_is_identity(self, gmail_handler, monkeypatch):
        monkeypatch.setattr("agentkernel.core.config.AKConfig.get", classmethod(lambda cls: make_gmail_cfg(conversation_initiation_enabled=False)))
        fake_service = FakeAgentService()
        monkeypatch.setattr("agentkernel.integration.gmail.gmail_chat.AgentService", lambda: fake_service)

        await gmail_handler._process_with_agent(sender="someone@example.com", subject="Hi", body="hello", session_id="thread-abc")

        assert fake_service.selected_session_id == "thread-abc"


class FakeGmailUsersMessages:
    def __init__(self, message: dict):
        self._message = message

    def get(self, userId, id, format):
        class _Exec:
            def __init__(self, result):
                self._result = result

            def execute(self):
                return self._result

        return _Exec(self._message)


class FakeGmailUsersThreads:
    def get(self, userId, id, format):
        class _Exec:
            def execute(self):
                return {"messages": []}

        return _Exec()


class FakeGmailUsers:
    def __init__(self, message: dict):
        self._messages = FakeGmailUsersMessages(message)
        self._threads = FakeGmailUsersThreads()

    def messages(self):
        return self._messages

    def threads(self):
        return self._threads


class FakeGmailService:
    def __init__(self, message: dict):
        self._users = FakeGmailUsers(message)

    def users(self):
        return self._users


def gmail_message(thread_id: str, sender: str = "someone@example.com", body_text: str = "hello") -> dict:
    import base64

    return {
        "threadId": thread_id,
        "payload": {
            "headers": [
                {"name": "Subject", "value": "Hi"},
                {"name": "From", "value": sender},
                {"name": "Message-ID", "value": "<abc@mail.gmail.com>"},
            ],
            "parts": [{"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(body_text.encode()).decode()}}],
        },
    }


class TestGmailProcessEmailResolution:
    """Site A (gmail_chat.py:267): drives the full _process_email path with a fake
    Gmail service double so resolve_session_id sees a real thread_id."""

    @pytest.mark.asyncio
    async def test_mapped_thread_resolves_to_session(self, gmail_handler, monkeypatch):
        InitiationManager.get()._store.save("session-1", "thread-abc")
        gmail_handler._service = FakeGmailService(gmail_message("thread-abc"))
        fake_service = FakeAgentService()
        monkeypatch.setattr("agentkernel.integration.gmail.gmail_chat.AgentService", lambda: fake_service)

        await gmail_handler._process_email("msg-1")

        assert fake_service.selected_session_id == "session-1"

    @pytest.mark.asyncio
    async def test_unmapped_thread_resolves_to_itself(self, gmail_handler, monkeypatch):
        gmail_handler._service = FakeGmailService(gmail_message("thread-new"))
        fake_service = FakeAgentService()
        monkeypatch.setattr("agentkernel.integration.gmail.gmail_chat.AgentService", lambda: fake_service)

        await gmail_handler._process_email("msg-2")

        assert fake_service.selected_session_id == "thread-new"
