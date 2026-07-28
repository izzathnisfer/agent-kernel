from types import SimpleNamespace

import pytest

from agentkernel import Agent, Runner
from agentkernel.core.initiation import InitiateConversationTool, InitiationManager
from agentkernel.core.initiation.tool import _initiate_conversation
from agentkernel.core.model import AgentReplyText, AgentRequestText
from agentkernel.core.runtime import Runtime
from agentkernel.core.session.in_memory import InMemorySessionStore
from agentkernel.core.session.mapping.in_memory import InMemoryMappingStore
from agentkernel.core.tool import SystemToolFactory, ToolContext


class DummyRunner(Runner):
    def __init__(self, name, reply_prefix="ok"):
        super().__init__(name)
        self._reply_prefix = reply_prefix

    async def run(self, agent, session, requests):
        prompt = requests[0].prompt if isinstance(requests[0], AgentRequestText) else ""
        return AgentReplyText(response=f"{self._reply_prefix}:{prompt}")

    async def stream(self, agent, session, requests):
        raise NotImplementedError()
        yield


class FailingRunner(DummyRunner):
    async def run(self, agent, session, requests):
        raise RuntimeError("model unavailable")


class DummyAgent(Agent):
    def __init__(self, name, runner=None):
        super().__init__(name, runner or DummyRunner("DummyRunner"))
        self._name = name

    @property
    def name(self):
        return self._name

    @property
    def runner(self):
        return self._runner

    def get_a2a_card(self):
        pass

    def get_description(self):
        pass

    def override_system_prompt(self, prompt):
        pass

    def attach_tool(self, tool):
        pass


def make_fake_cfg(conversation_initiation_enabled=True):
    class FakeCfg:
        multimodal = None
        thread = None

        class session:
            type = "in_memory"
            cache = None

        class guardrail:
            class input:
                enabled = False

            class output:
                enabled = False

    FakeCfg.conversation_initiation_enabled = conversation_initiation_enabled
    FakeCfg.session.initiation = SimpleNamespace(enabled=conversation_initiation_enabled, store=None)
    return FakeCfg


@pytest.fixture(autouse=True)
def reset_state():
    Runtime._system_pre_hooks = None
    Runtime._system_post_hooks = None
    InitiationManager.reset()
    InMemoryMappingStore().clear()
    yield
    Runtime._system_pre_hooks = None
    Runtime._system_post_hooks = None
    InitiationManager.reset()
    InMemoryMappingStore().clear()


@pytest.fixture
def enabled_cfg(monkeypatch):
    monkeypatch.setattr("agentkernel.core.config.AKConfig.get", classmethod(lambda cls: make_fake_cfg()))


@pytest.fixture
def runtime(enabled_cfg):
    runtime = Runtime(InMemorySessionStore())
    runtime.register(DummyAgent("first-agent"))
    caller_session = runtime.sessions().new("caller-session")
    tool_ctx = ToolContext(runtime, runtime.agents()["first-agent"], caller_session, []).set()
    yield runtime
    tool_ctx.reset()


@pytest.fixture
def dispatched(runtime):
    received = []
    InitiationManager.register_dispatcher(received.append)
    return received


def session_id_from(result: str) -> str:
    assert result.startswith("Conversation initiated. session_id="), result
    return result.split("session_id=", 1)[1]


class TestInitiateConversation:
    def test_creates_session_runs_agent_and_dispatches(self, runtime, dispatched):
        result = _initiate_conversation(target="U123", prompt="Inform Monroe her laptop is ready")

        session_id = session_id_from(result)
        assert len(dispatched) == 1
        initiation = dispatched[0]
        assert initiation.session_id == session_id
        assert initiation.message == "ok:Inform Monroe her laptop is ready"
        assert initiation.user_id == "U123"  # defaults to target
        assert initiation.target == "U123"
        assert initiation.target_details is None
        assert initiation.request_id
        # The new session was created and persisted by the runner-side agent run
        runtime.sessions().load(session_id, strict=True)

    def test_explicit_user_id(self, runtime, dispatched):
        _initiate_conversation(target="U123", prompt="hi", user_id="monroe")
        assert dispatched[0].user_id == "monroe"
        assert dispatched[0].target_details is None  # only custom dispatch paths can carry platform extras

    def test_named_agent_composes_the_message(self, runtime, dispatched):
        runtime.register(DummyAgent("notifier", runner=DummyRunner("NotifierRunner", reply_prefix="notify")))
        _initiate_conversation(target="U123", prompt="hi", agent="notifier")
        assert dispatched[0].message == "notify:hi"

    def test_default_agent_is_first_registered(self, runtime, dispatched):
        runtime.register(DummyAgent("second-agent", runner=DummyRunner("SecondRunner", reply_prefix="second")))
        _initiate_conversation(target="U123", prompt="hi")
        assert dispatched[0].message == "ok:hi"

    def test_unknown_agent_returns_error_text(self, runtime, dispatched):
        result = _initiate_conversation(target="U123", prompt="hi", agent="nope")
        assert result == "Cannot initiate conversation: no agent named 'nope' is registered"
        assert dispatched == []

    def test_missing_target_or_prompt_returns_error_text(self, runtime, dispatched):
        assert _initiate_conversation(target="", prompt="hi") == "Cannot initiate conversation: 'target' is required"
        assert _initiate_conversation(target="U123", prompt="") == "Cannot initiate conversation: 'prompt' is required"
        assert dispatched == []

    def test_disabled_feature_returns_error_text(self, monkeypatch):
        monkeypatch.setattr("agentkernel.core.config.AKConfig.get", classmethod(lambda cls: make_fake_cfg(conversation_initiation_enabled=False)))
        result = _initiate_conversation(target="U123", prompt="hi")
        assert "not enabled" in result

    def test_no_dispatcher_returns_error_text(self, runtime):
        result = _initiate_conversation(target="U123", prompt="hi")
        assert "no initiation dispatcher" in result

    def test_agent_run_failure_returns_error_text(self, runtime, dispatched):
        runtime.register(DummyAgent("broken", runner=FailingRunner("FailingRunner")))
        result = _initiate_conversation(target="U123", prompt="hi", agent="broken")
        assert "composing the outbound message failed" in result
        assert "model unavailable" in result
        assert dispatched == []

    def test_no_tool_context_returns_error_text(self, enabled_cfg):
        InitiationManager.register_dispatcher(lambda initiation: None)
        result = _initiate_conversation(target="U123", prompt="hi")
        assert result.startswith("Cannot initiate conversation:")
        assert "ToolContext" in result


class TestSystemToolRegistration:
    def test_registered_when_enabled(self, enabled_cfg):
        tools = SystemToolFactory.get_all()
        assert any(isinstance(tool, InitiateConversationTool) for tool in tools)
        assert "initiate_conversation" in SystemToolFactory.get_system_prompt_suffix()

    def test_not_registered_when_disabled(self, monkeypatch):
        monkeypatch.setattr("agentkernel.core.config.AKConfig.get", classmethod(lambda cls: make_fake_cfg(conversation_initiation_enabled=False)))
        assert not any(isinstance(tool, InitiateConversationTool) for tool in SystemToolFactory.get_all())


class TestLocalRESTDispatch:
    """RESTAPI._register_initiation_sender wires an InitiationSender handler as the
    in-process send point (single-process REST deployments)."""

    def make_initiation(self):
        from agentkernel.core.initiation import InitiationMessage

        return InitiationMessage(session_id="session-7", message="hello", target="U123", user_id="monroe", request_id="req-7")

    def make_sender(self):
        from agentkernel.core.initiation import InitiationSender

        class FakeSender(InitiationSender):
            def __init__(self):
                self.sent = []

            def send_initiation_message(self, target, message, target_details=None):
                self.sent.append((target, message, target_details))
                return "thread-77"

        return FakeSender()

    def test_sender_delivers_and_completes(self, enabled_cfg):
        from agentkernel.api.http import RESTAPI

        sender = self.make_sender()
        RESTAPI._register_initiation_sender([sender])

        manager = InitiationManager.get()
        manager.dispatch(self.make_initiation())

        assert sender.sent == [("U123", "hello", None)]
        assert manager.resolve_session_id("thread-77") == "session-7"
        assert manager.get_messaging_integration_thread_id("session-7") == "thread-77"

    def test_no_sender_registers_nothing(self, enabled_cfg):
        from agentkernel.api.http import RESTAPI

        RESTAPI._register_initiation_sender([])
        with pytest.raises(ValueError, match="dispatcher"):
            InitiationManager.get().dispatch(self.make_initiation())

    def test_first_of_multiple_senders_wins(self, enabled_cfg):
        from agentkernel.api.http import RESTAPI

        first, second = self.make_sender(), self.make_sender()
        RESTAPI._register_initiation_sender([first, second])

        InitiationManager.get().dispatch(self.make_initiation())

        assert len(first.sent) == 1
        assert second.sent == []
