from agent import AGENTS


def test_multi_agent_handoff_topology_is_wired():
    agents = {agent.name: agent for agent in AGENTS}
    assert set(agents) == {
        "rescuemesh",
        "rescuemesh_incident_intake",
        "rescuemesh_verifier",
        "rescuemesh_resource_desk",
        "rescuemesh_coordinator",
    }
    assert {handoff.name for handoff in agents["rescuemesh"].handoffs} == {
        "rescuemesh_incident_intake",
        "rescuemesh_verifier",
        "rescuemesh_resource_desk",
        "rescuemesh_coordinator",
    }


def test_specialists_have_domain_tools_and_router_does_not():
    agents = {agent.name: agent for agent in AGENTS}
    assert agents["rescuemesh"].tools == []
    coordinator_tools = {tool.name for tool in agents["rescuemesh_coordinator"].tools}
    assert {"match_resources", "network_allocation_plan", "confirm_match", "operations_snapshot"} <= coordinator_tools
    incident_tools = {tool.name for tool in agents["rescuemesh_incident_intake"].tools}
    assert "report_incident" in incident_tools
