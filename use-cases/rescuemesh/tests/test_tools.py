import json

import pytest

from tools import (
    confirm_match,
    match_resources,
    operations_snapshot,
    public_incident_brief,
    register_resource,
    report_incident,
    reset_demo_state,
    verify_incident,
)


@pytest.fixture(autouse=True)
def clean_state():
    reset_demo_state()
    yield
    reset_demo_state()


def _incident(**overrides):
    payload = {
        "location": "No. 44 Temple Lane, Katubedda, Moratuwa",
        "needs": "rescue, drinking water",
        "people_count": 6,
        "severity": "high",
        "vulnerable_groups": "elderly person and children",
        "reporter_contact": "071 555 0199",
        "notes": "Call me at 071 555 0199 if needed.",
    }
    payload.update(overrides)
    return json.loads(report_incident(**payload))


def test_priority_is_transparent_and_high_risk_case_gets_p1_or_p2():
    incident = _incident()["incident"]
    assert incident["priority_score"] >= 55
    assert incident["priority_band"].startswith(("P1", "P2"))
    assert incident["priority_reasons"]


def test_duplicate_reports_are_merged_instead_of_creating_noise():
    first = _incident()
    duplicate = _incident(location="Temple Lane Katubedda Moratuwa", people_count=5)
    assert duplicate["result"] == "merged_with_likely_duplicate"
    assert duplicate["duplicate_of"] == first["incident"]["incident_id"]
    assert duplicate["incident"]["report_count"] == 2
    snapshot = json.loads(operations_snapshot())
    assert snapshot["incidents_total"] == 1
    assert snapshot["duplicate_reports_merged"] == 1


def test_public_brief_removes_contact_and_fine_grained_house_number():
    incident_id = _incident()["incident"]["incident_id"]
    brief = json.loads(public_incident_brief(incident_id))
    serialized = json.dumps(brief)
    assert "071 555 0199" not in serialized
    assert "No. 44" not in serialized
    assert "reporter_contact" not in brief


def test_verification_changes_status_without_autonomous_dispatch():
    incident_id = _incident()["incident"]["incident_id"]
    result = json.loads(
        verify_incident(
            incident_id,
            verification_type="eyewitness",
            evidence_summary="Trusted volunteer confirms water at the entrance.",
            verifier="Volunteer lead",
        )
    )
    assert result["incident"]["verified"] is True
    assert result["incident"]["status"] == "verified"
    assert result["incident"]["matched_resources"] == []


def test_matching_is_proposal_until_human_confirmation():
    incident_id = _incident()["incident"]["incident_id"]
    resource = json.loads(
        register_resource(
            provider_name="Kayak Club",
            resource_type="boat",
            quantity=1,
            location="Katubedda Moratuwa",
            availability_minutes=15,
        )
    )["resource"]
    matches = json.loads(match_resources(incident_id))
    assert matches["proposed_matches"][0]["resource_id"] == resource["resource_id"]
    assert "human" in matches["dispatch_policy"].lower()

    confirmed = json.loads(confirm_match(incident_id, resource["resource_id"], reviewer="Ops lead"))
    assert confirmed["result"] == "match_confirmed"
    snapshot = json.loads(operations_snapshot())
    assert snapshot["confirmed_matches"] == 1
