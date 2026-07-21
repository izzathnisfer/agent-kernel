"""
Agent Runner Lambda — runs the two-agent Slack notification flow and carries
Slack delivery context (channel, thread_ts) from the Input Queue to the Output
Queue via custom SQS attributes. ChatService's response body has no room for
platform routing data, so it has to ride along on the message itself; see
lambda_request_handler.py for where these attributes are attached and
lambda_response_handler.py for where they're read back to deliver the reply.

Reuses examples/api/slack-initiation/server.py's two-agent identity-grounding
design unchanged: "general" faces the requester (extracts the recipient's
member id from the Slack mention, calls get_requester_id, composes a
third-person prompt naming both explicitly), "notifier" faces the recipient
(writes the outbound notification itself, in third person, correctly
attributed) — see that file's module docstring for the full rationale.
"""

from agentkernel.aws import ServerlessAgentRunner, SQSHandler
from agentkernel.core import AgentRequestAny, ToolContext
from agentkernel.openai import OpenAIModule, OpenAIToolBuilder
from agents import Agent as OpenAIAgent


def get_requester_id() -> str:
    """Returns the Slack member id of whoever sent the current message, or "" if unknown."""
    for req in ToolContext.get().requests:
        if isinstance(req, AgentRequestAny) and req.name == "body":
            return req.content.get("user", "")
    return ""


general_agent = OpenAIAgent(
    name="general",
    handoff_description="Agent for general questions",
    instructions=(
        "You provide assistance with general queries. Give short and clear answers.\n"
        "When asked to message, notify, or inform another user, follow these steps exactly:\n"
        "1. Extract the recipient's member id from the Slack mention in the message text (mentions arrive "
        "as <@MEMBERID>, e.g. <@U0AB12CD3>).\n"
        "2. Call get_requester_id exactly once to learn who is actually asking (Slack message text never "
        "self-mentions the sender, so this cannot be read from the text).\n"
        '3. Compose `prompt` as third-person background naming both explicitly — e.g. "<@U0REQUESTER> '
        'asked you to let <@U0RECIPIENT> know they will be late because of traffic." Never phrase it in '
        'first person ("I will be late") — the notifier is not the requester. If get_requester_id '
        "returns nothing, say the requester is unidentified rather than guessing.\n"
        "4. Call initiate_conversation exactly once, with `target` set to the recipient's member id "
        "(never the requester's own id) and agent='notifier' — never call this tool more than once per "
        "request.\n"
        "If the request names a person without a Slack mention or member id, ask the requester to "
        "@-mention them instead of guessing who they mean."
    ),
    tools=OpenAIToolBuilder.bind([get_requester_id]),
)

notifier_agent = OpenAIAgent(
    name="notifier",
    handoff_description="Composes outbound notification messages",
    instructions=(
        "You write messages that are delivered to a recipient verbatim, based on third-person background "
        'like "<@U0REQUESTER> asked you to let <@U0RECIPIENT> know they will be late because of traffic." '
        "Reply with exactly the text the recipient should read: a direct, friendly notification of one or "
        'two sentences, attributed to the requester by their mention (e.g. "Hi! Just a heads up — '
        '<@U0REQUESTER> will be late, held up in traffic."). Never write in first person as if you are the '
        "requester or the one the fact is about — you are relaying it on their behalf. Never reply with "
        'meta commentary like "Sure, I can send that" — your reply IS the message.\n'
        "Remember the requester's identity and the reason from this background: if the recipient later "
        'asks something like "who said that?" or "why?", answer from these facts (e.g. "<@U0REQUESTER> '
        'did." / "Traffic."), not from who is currently asking — the recipient asking the question is '
        "never the requester."
    ),
)

OpenAIModule([general_agent, notifier_agent])


class AgentRunner(ServerlessAgentRunner):
    """Carries Slack channel/thread_ts from the input record's custom attributes
    to the output record's, since ChatService's response body carries no
    platform routing data — see the module docstring."""

    @classmethod
    def _get_record_attributes(cls, raw_queue_message: dict) -> dict:
        record_attributes = super()._get_record_attributes(raw_queue_message)
        incoming = SQSHandler.get_message_custom_attributes(raw_queue_message)
        record_attributes["channel"] = incoming.get("channel")
        record_attributes["thread_ts"] = incoming.get("thread_ts")
        return record_attributes

    @classmethod
    def _send_to_output_queue(cls, message_body: dict, record_attributes: dict) -> None:
        custom_attributes = []
        if record_attributes.get("channel") is not None:
            custom_attributes.append(
                SQSHandler.CustomAttribute(
                    name="channel", value=record_attributes["channel"], datatype=SQSHandler.AttributeDataType.STRING
                )
            )
        if record_attributes.get("thread_ts") is not None:
            custom_attributes.append(
                SQSHandler.CustomAttribute(
                    name="thread_ts", value=record_attributes["thread_ts"], datatype=SQSHandler.AttributeDataType.STRING
                )
            )

        SQSHandler.send_message_to_output_queue(
            message_body=message_body,
            attributes={
                "message_group_id": record_attributes["message_group_id"],
                "message_deduplication_id": record_attributes["message_deduplication_id"],
            },
            request_id=record_attributes["request_id"],
            user_id=record_attributes["user_id"],
            custom_message_attributes=custom_attributes,
        )


handler = AgentRunner.handle
