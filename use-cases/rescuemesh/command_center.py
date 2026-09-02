from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("AK_CONFIG_PATH_OVERRIDE", str(Path(__file__).with_name("config.command-center.yaml")))
os.environ.setdefault("RESCUEMESH_LEDGER_PATH", str(Path(__file__).with_name(".rescuemesh") / "ledger.json"))

from agentkernel.api import RESTAPI
from agentkernel.openai import OpenAIModule
from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from agent import AGENTS
from tools import (
    command_center_snapshot,
    confirm_match,
    network_allocation_plan,
    register_resource,
    report_incident,
    reset_demo_state,
    verify_incident,
)


class ConfirmRequest(BaseModel):
    incident_id: str
    resource_id: str
    reviewer: str = "Judge demo coordinator"


def _data(value: str) -> dict:
    return json.loads(value)


def _incident_id(result: str) -> str:
    payload = _data(result)
    return payload.get("incident", {}).get("incident_id") or payload.get("duplicate_of", "")


def seed_judge_scenario() -> dict:
    """Create a deterministic multi-incident scenario that exercises network allocation."""
    reset_demo_state()

    hostel = _incident_id(
        report_incident(
            "No. 12 Hostel Lane, Katubedda, Moratuwa",
            "rescue, drinking water",
            people_count=6,
            severity="high",
            vulnerable_groups="two children and one elderly person",
            reporter_contact="071 555 0199",
            notes="Floodwater is rising and ground-floor access is blocked.",
        )
    )
    report_incident(
        "12 Hostel Lane, Katubedda, Moratuwa",
        "boat rescue and clean water",
        people_count=6,
        severity="high",
        notes="Neighbour reports the same flooded hostel.",
    )
    verify_incident(
        hostel,
        "trusted volunteer eyewitness",
        "Campus response volunteer visually confirmed flooding from the lane entrance.",
        "Campus response lead",
    )

    clinic = _incident_id(
        report_incident(
            "Community Clinic, Egoda Uyana, Moratuwa",
            "medical transport, power",
            people_count=11,
            severity="critical",
            vulnerable_groups="injured patients",
            notes="Road access is partially blocked and backup power is low.",
        )
    )
    verify_incident(
        clinic,
        "facility staff confirmation",
        "Clinic duty officer confirmed patients and backup-power constraints.",
        "Area coordinator",
    )

    shelter = _incident_id(
        report_incident(
            "Riverside Community Hall, Moratuwa",
            "shelter, food, water",
            people_count=22,
            severity="moderate",
            vulnerable_groups="families with children",
            notes="Displaced families are arriving; needs are still being verified.",
        )
    )

    register_resource("Moratuwa Kayak Club", "boat", 1, "Katubedda, Moratuwa", 20, "077 123 4567")
    register_resource("St John Volunteer Unit", "ambulance", 1, "Moratuwa", 25, "071 777 3030")
    register_resource("Engineering Society", "generator", 1, "University of Moratuwa", 35, "076 404 1010")
    register_resource("Student Union Relief Point", "drinking water", 40, "Katubedda, Moratuwa", 10, "075 313 2020")
    register_resource("Community Kitchen", "food", 60, "Moratuwa", 45, "070 909 0001")
    register_resource("Temple Relief Hall", "shelter", 25, "Moratuwa", 15, "072 111 2200")

    return {
        "scenario": {"hostel": hostel, "clinic": clinic, "shelter": shelter},
        "board": _data(command_center_snapshot()),
        "allocation": _data(network_allocation_plan()),
    }


router = APIRouter()


@router.get("/rescuemesh", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    html = Path(__file__).with_name("command_center.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@router.get("/rescuemesh/api/state")
def state() -> dict:
    return _data(command_center_snapshot())


@router.get("/rescuemesh/api/allocation")
def allocation() -> dict:
    return _data(network_allocation_plan())


@router.post("/rescuemesh/api/demo/seed")
def seed() -> dict:
    return seed_judge_scenario()


@router.post("/rescuemesh/api/demo/reset")
def reset() -> dict:
    reset_demo_state()
    return _data(command_center_snapshot())


@router.post("/rescuemesh/api/confirm")
def confirm(request: ConfirmRequest) -> dict:
    return _data(confirm_match(request.incident_id, request.resource_id, request.reviewer))


OpenAIModule(AGENTS)
RESTAPI.add(router)


if __name__ == "__main__":
    RESTAPI.run()
