import json

import tools
from tools import (
    command_center_snapshot,
    network_allocation_plan,
    register_resource,
    report_incident,
    reset_demo_state,
    verify_incident,
)


def _payload(value: str) -> dict:
    return json.loads(value)


def _incident_id(value: str) -> str:
    return _payload(value)["incident"]["incident_id"]


def test_network_plan_never_double_proposes_a_resource():
    reset_demo_state()
    urgent = _incident_id(report_incident("Katubedda", "rescue, water", 8, "critical"))
    verify_incident(urgent, "eyewitness", "confirmed", "coordinator")
    lower = _incident_id(report_incident("Moratuwa", "shelter, water", 4, "moderate"))
    register_resource("Boat club", "boat", 1, "Katubedda", 10)
    register_resource("Water point", "drinking water", 20, "Moratuwa", 10)
    register_resource("Hall", "shelter", 10, "Moratuwa", 10)

    plan = _payload(network_allocation_plan())
    resource_ids = [item["resource_id"] for item in plan["assignments"]]
    assert len(resource_ids) == len(set(resource_ids))
    assert plan["mode"] == "dry_run_network_allocation"
    assert urgent in {item["incident_id"] for item in plan["assignments"]}
    assert lower in {item["incident_id"] for item in plan["assignments"]}


def test_command_center_snapshot_is_privacy_safe():
    reset_demo_state()
    incident_id = _incident_id(
        report_incident(
            "No. 42 Riverside Lane, Moratuwa",
            "water",
            3,
            "high",
            reporter_contact="071 555 0199",
            notes="Call 071 555 0199 at house 42.",
        )
    )
    board = _payload(command_center_snapshot())
    encoded = json.dumps(board)
    incident = next(item for item in board["incidents"] if item["incident_id"] == incident_id)
    assert "071 555 0199" not in encoded
    assert "reporter_contact" not in encoded
    assert "42" not in incident["location"]


def test_optional_ledger_path_survives_process_state_reset(tmp_path, monkeypatch):
    ledger = tmp_path / "rescuemesh.json"
    monkeypatch.setenv("RESCUEMESH_LEDGER_PATH", str(ledger))
    reset_demo_state()
    incident_id = _incident_id(report_incident("Moratuwa", "food", 2, "moderate"))
    assert ledger.exists()

    # Simulate a fresh process memory image while leaving the persisted ledger intact.
    tools._SHARED_STATE = tools._new_state()
    recovered = _payload(command_center_snapshot())
    assert any(item["incident_id"] == incident_id for item in recovered["incidents"])
