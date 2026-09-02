import json
from concurrent.futures import ThreadPoolExecutor

from tools import (
    command_center_snapshot,
    reset_demo_state,
    submit_field_incident,
    submit_field_resource,
)


def _data(value: str) -> dict:
    return json.loads(value)


def setup_function():
    reset_demo_state()


def test_field_incident_retries_are_idempotent():
    first = _data(
        submit_field_incident(
            "mobile-incident-001",
            "Katubedda, Moratuwa",
            "rescue, water",
            people_count=4,
            severity="high",
            reporter_contact="071 555 0101",
        )
    )
    second = _data(
        submit_field_incident(
            "mobile-incident-001",
            "Katubedda, Moratuwa",
            "rescue, water",
            people_count=4,
            severity="high",
            reporter_contact="071 555 0101",
        )
    )
    board = _data(command_center_snapshot())
    assert first["result"] == "created"
    assert second["result"] == "idempotent_replay"
    assert second["incident"]["incident_id"] == first["incident"]["incident_id"]
    assert "reporter_contact" not in first["incident"]
    assert "reporter_contact" not in second["incident"]
    assert board["metrics"]["incidents_total"] == 1


def test_field_resource_retries_do_not_duplicate_inventory():
    first = _data(
        submit_field_resource("mobile-resource-001", "Kayak Club", "boat", 1, "Katubedda", 15, contact="077 123 4567")
    )
    second = _data(
        submit_field_resource("mobile-resource-001", "Kayak Club", "boat", 1, "Katubedda", 15, contact="077 123 4567")
    )
    board = _data(command_center_snapshot())
    assert first["result"] == "registered"
    assert second["result"] == "idempotent_replay"
    assert second["resource"]["resource_id"] == first["resource"]["resource_id"]
    assert "contact" not in first["resource"]
    assert "contact" not in second["resource"]
    assert board["metrics"]["available_resources"] == 1


def test_concurrent_field_incident_retries_create_one_incident():
    def submit_once(_):
        return _data(
            submit_field_incident(
                "mobile-concurrent-incident",
                "Katubedda, Moratuwa",
                "rescue, water",
                people_count=7,
                severity="high",
            )
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(submit_once, range(16)))

    incident_ids = {result["incident"]["incident_id"] for result in results}
    board = _data(command_center_snapshot())
    assert len(incident_ids) == 1
    assert sum(result["result"] == "created" for result in results) == 1
    assert board["metrics"]["incidents_total"] == 1
    assert board["incidents"][0]["report_count"] == 1


def test_concurrent_field_resource_retries_create_one_inventory_item():
    def submit_once(_):
        return _data(
            submit_field_resource(
                "mobile-concurrent-resource",
                "Kayak Club",
                "boat",
                1,
                "Katubedda",
                15,
            )
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(submit_once, range(16)))

    resource_ids = {result["resource"]["resource_id"] for result in results}
    board = _data(command_center_snapshot())
    assert len(resource_ids) == 1
    assert sum(result["result"] == "registered" for result in results) == 1
    assert board["metrics"]["available_resources"] == 1
