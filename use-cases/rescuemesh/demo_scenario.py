import json

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


def show(title: str, payload: str) -> dict:
    print(f"\n=== {title} ===")
    data = json.loads(payload)
    print(json.dumps(data, indent=2))
    return data


def main() -> None:
    reset_demo_state()

    first = show(
        "1. First community report",
        report_incident(
            location="No. 12 Hostel Lane, Katubedda, Moratuwa",
            needs="rescue, drinking water",
            people_count=6,
            severity="high",
            vulnerable_groups="one elderly person and two children",
            reporter_contact="071 555 0199",
            notes="Floodwater is rising; ground-floor access is blocked.",
        ),
    )
    incident_id = first["incident"]["incident_id"]

    show(
        "2. Likely duplicate report is merged",
        report_incident(
            location="Hostel Lane Katubedda Moratuwa",
            needs="boat evacuation and clean water",
            people_count=5,
            severity="high",
            vulnerable_groups="children",
            notes="A second neighbour reports the same flooded hostel.",
        ),
    )

    show(
        "3. Human/community verification is recorded",
        verify_incident(
            incident_id,
            verification_type="trusted volunteer eyewitness",
            evidence_summary="University volunteer reached the lane entrance and visually confirmed flooding.",
            verifier="Campus response lead",
        ),
    )

    boat = show(
        "4. Volunteer boat is registered",
        register_resource(
            provider_name="Moratuwa Kayak Club",
            resource_type="boat",
            quantity=1,
            location="Katubedda, Moratuwa",
            availability_minutes=20,
            contact="077 123 4567",
            notes="Two trained paddlers available.",
        ),
    )
    boat_id = boat["resource"]["resource_id"]

    show(
        "5. Drinking water is registered",
        register_resource(
            provider_name="Student Union Relief Point",
            resource_type="drinking water",
            quantity=40,
            location="University of Moratuwa, Katubedda",
            availability_minutes=10,
            notes="40 sealed 1L bottles.",
        ),
    )

    show("6. RescueMesh proposes ranked matches", match_resources(incident_id))
    show(
        "7. Human coordinator confirms the boat match",
        confirm_match(incident_id, boat_id, reviewer="Relief desk coordinator"),
    )
    show("8. Privacy-safe public brief", public_incident_brief(incident_id))
    show("9. Operations snapshot", operations_snapshot())


if __name__ == "__main__":
    main()
