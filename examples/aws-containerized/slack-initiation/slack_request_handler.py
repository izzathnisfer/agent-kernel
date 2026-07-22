"""
Slack webhook receiver for the ECS containerized deployment.

Reuses slack_bolt's AsyncApp/AsyncSlackRequestHandler exactly as
AgentSlackRequestHandler does (ak-py/src/agentkernel/integration/slack/slack_chat.py)
— ECS is FastAPI-based, unlike Lambda's raw event dispatch, so the same Bolt
signature-verification and event-routing machinery applies here. The
difference is handle(): instead of running the agent synchronously in-process
(as the single-process REST example does), it resolves the session id and
enqueues to the Input Queue, returning immediately — a queue deployment must
not block the webhook response on the full agent round trip, which happens in
a separate process (app_agent_runner.py) here.

See slack_output_consumer.py for how the reply reaches Slack, and the module
docstring there for why channel/thread_ts are attached as custom SQS
attributes below.
"""

import logging

from agentkernel.api import RESTRequestHandler
from agentkernel.aws import SQSHandler
from agentkernel.core import Config
from fastapi import APIRouter, Request
from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler
from slack_bolt.async_app import AsyncApp


class SlackECSRequestHandler(RESTRequestHandler):
    """
    API router that receives Slack events and enqueues them for the Agent
    Runner. Endpoints:
    - GET /health: Health check
    - POST /slack/events: Handle Slack events
    """

    def __init__(self):
        self._log = logging.getLogger("ak.example.slack_initiation.request_handler")
        self._slack_agent = Config.get().slack.agent if Config.get().slack.agent != "" else None
        self._bot_id = None

        self._slack_app = AsyncApp()
        self._handler = AsyncSlackRequestHandler(self._slack_app)
        slack_app = self._slack_app

        @slack_app.event("message")
        async def handle_messages(message, say):
            await self.handle(message, say)

    def get_router(self) -> APIRouter:
        router = APIRouter()

        @router.get("/health")
        def health():
            return {"status": "ok"}

        @router.post("/slack/events")
        async def slack_events(req: Request):
            return await self._handler.handle(req)

        return router

    async def handle(self, body: dict, say) -> None:
        """
        :param body: dict containing Slack message data.
        :param say: function for sending messages back to Slack (unused here —
            replies are delivered later, out of band, by SlackECSOutputConsumer).
        """
        user = body.get("user")
        text = (body.get("text") or "").strip()
        channel = body.get("channel")

        if self._bot_id is None:
            self._bot_id = (await self._slack_app.client.auth_test())["user_id"]

        # Avoid the bot responding to its own messages (and re-enqueueing them forever).
        if user == self._bot_id:
            return

        if not text:
            return

        # thread_ts is populated for a threaded reply; otherwise fall back to
        # the message's own ts — a fresh id every time, so an un-threaded
        # reply always starts a new session (see slack_chat.py's handle() for
        # the identical rule the single-process REST example follows).
        thread_ts = body.get("thread_ts") or body.get("ts")
        session_id = self.resolve_session_id(thread_ts)

        queue_url = Config.get().execution.queues.input.url
        if not queue_url:
            self._log.error("Input queue URL is not configured")
            return

        # ts (not thread_ts) as the fallback: thread_ts is shared by every reply in
        # the same thread, so using it as message_deduplication_id would collide two
        # thread replies inside SQS's 5-minute dedup window and silently drop one.
        request_id = body.get("client_msg_id") or body["ts"]

        # The raw Slack event rides along as an extra "body" field: ChatService
        # turns unknown body fields into AgentRequestAny entries on the agent's
        # ToolContext, which is how the get_requester_id tool (app_agent_runner.py)
        # learns who sent the message — same contract as the REST example.
        SQSHandler.send_message_to_input_queue(
            message_body={"prompt": text, "agent": self._slack_agent, "session_id": session_id, "body": body},
            attributes={"message_group_id": session_id, "message_deduplication_id": request_id},
            request_id=request_id,
            user_id=user,
            custom_message_attributes=[
                SQSHandler.CustomAttribute(name="channel", value=channel, datatype=SQSHandler.AttributeDataType.STRING),
                SQSHandler.CustomAttribute(
                    name="thread_ts", value=thread_ts, datatype=SQSHandler.AttributeDataType.STRING
                ),
            ],
        )
        self._log.info(f"Enqueued Slack message — session_id={session_id} request_id={request_id} channel={channel}")
