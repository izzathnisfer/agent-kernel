import json

from agentkernel.core import Runtime, Session, ToolContext

from agent import AGENTS
from tools import match_resources, register_resource, report_incident, reset_demo_state


def _in_context(agent_name: str, session: Session, fn):
    agent = next(agent for agent in AGENTS if agent.name == agent_name)
    with ToolContext(runtime=Runtime.current(), agent=agent, session=session, requests=[]) as context:
        context.set()
        try:
            return fn()
        finally:
            context.reset()


def test_agentkernel_session_remembers_incident_while_community_state_crosses_sessions():
    reset_demo_state()
    reporter = Session("resident-chat")
    donor = Session("volunteer-chat")

    created = json.loads(
        _in_context(
            "rescuemesh_incident_intake",
            reporter,
            lambda: report_incident("Katubedda, Moratuwa", "boat rescue", 5, "high"),
        )
    )
    incident_id = created["incident"]["incident_id"]

    _in_context(
        "rescuemesh_resource_desk",
        donor,
        lambda: register_resource("Kayak Club", "boat", 1, "Katubedda, Moratuwa", 15),
    )

    matches = json.loads(
        _in_context(
            "rescuemesh_coordinator",
            reporter,
            lambda: match_resources(""),
        )
    )
    assert matches["incident_id"] == incident_id
    assert matches["proposed_matches"][0]["resource_type"] == "boat"
