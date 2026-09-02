from __future__ import annotations

import copy
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from agentkernel.core import ToolContext

SESSION_LAST_INCIDENT_KEY = "rescuemesh.last_incident_id"
SESSION_LAST_RESOURCE_KEY = "rescuemesh.last_resource_id"
SESSION_LAST_AREA_KEY = "rescuemesh.last_area"
ACTIVE_STATUSES = {"open", "verified", "matched", "responding"}
ALLOWED_STATUSES = ACTIVE_STATUSES | {"resolved", "closed"}

SEVERITY_POINTS = {
    "low": 10,
    "moderate": 30,
    "high": 50,
    "critical": 70,
}

NEED_KEYWORDS = {
    "rescue": {"rescue", "evacuation", "trapped", "boat", "transport"},
    "medical": {"medical", "medicine", "injured", "first aid", "ambulance"},
    "water": {"water", "drinking water", "clean water"},
    "food": {"food", "meals", "rations"},
    "shelter": {"shelter", "housing", "accommodation", "blankets"},
    "power": {"power", "electricity", "charging", "generator"},
}

RESOURCE_ALIASES = {
    "boat": {"rescue", "evacuation", "transport"},
    "vehicle": {"rescue", "evacuation", "transport"},
    "ambulance": {"medical", "rescue", "transport"},
    "first aid": {"medical"},
    "medicine": {"medical"},
    "drinking water": {"water"},
    "water": {"water"},
    "food": {"food"},
    "meals": {"food"},
    "shelter": {"shelter"},
    "blankets": {"shelter"},
    "generator": {"power"},
    "power bank": {"power"},
}

_SHARED_STATE: dict[str, Any] = {
    "incidents": {},
    "resources": {},
    "timeline": [],
}
_LEDGER_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_state() -> dict[str, Any]:
    return {"incidents": {}, "resources": {}, "timeline": []}


def _session_cache():
    try:
        return ToolContext.get().session.get_non_volatile_cache()
    except (RuntimeError, AttributeError):
        return None


def _ledger_path() -> Path | None:
    value = os.environ.get("RESCUEMESH_LEDGER_PATH", "").strip()
    return Path(value).expanduser() if value else None


def _get_state() -> dict[str, Any]:
    # Community incidents/resources must be visible across Telegram/chat sessions. By default the
    # ledger is process-shared; setting RESCUEMESH_LEDGER_PATH adds atomic JSON persistence so a
    # local command center or bot can survive a restart without changing the Agent Kernel sessions.
    path = _ledger_path()
    with _LEDGER_LOCK:
        if path is not None and path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and all(key in loaded for key in ("incidents", "resources", "timeline")):
                    return copy.deepcopy(loaded)
            except (OSError, json.JSONDecodeError):
                pass
        return copy.deepcopy(_SHARED_STATE)


def _save_state(state: dict[str, Any]) -> None:
    global _SHARED_STATE
    snapshot = copy.deepcopy(state)
    path = _ledger_path()
    with _LEDGER_LOCK:
        _SHARED_STATE = snapshot
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
            tmp.replace(path)


def _remember_session(key: str, value: str) -> None:
    cache = _session_cache()
    if cache is not None:
        cache.set(key, value)


def _recall_session(key: str) -> str:
    cache = _session_cache()
    if cache is None:
        return ""
    value = cache.get(key, "")
    return str(value) if value else ""


def _resolve_incident_id(incident_id: str) -> str:
    return incident_id.strip() or _recall_session(SESSION_LAST_INCIDENT_KEY)


def reset_demo_state() -> None:
    """Reset the process-shared ledger. Intended for tests and the deterministic demo only."""
    _save_state(_new_state())


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def _tokens(value: str) -> set[str]:
    stop = {"the", "a", "an", "at", "in", "on", "near", "road", "street", "lane", "area"}
    return {token for token in re.findall(r"[a-z0-9]+", _normalize(value)) if len(token) > 1 and token not in stop}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _need_tags(needs: str) -> set[str]:
    normalized = _normalize(needs)
    tags: set[str] = set()
    for tag, keywords in NEED_KEYWORDS.items():
        if any(keyword in normalized for keyword in keywords):
            tags.add(tag)
    if not tags:
        tags.update(token for token in re.split(r"[,;/]", normalized) if token.strip())
    return {tag.strip() for tag in tags if tag.strip()}


def _priority_score(people_count: int, severity: str, needs: str, vulnerable_groups: str) -> tuple[int, str, list[str]]:
    severity_key = _normalize(severity)
    score = SEVERITY_POINTS.get(severity_key, SEVERITY_POINTS["moderate"])
    reasons = [f"severity={severity_key if severity_key in SEVERITY_POINTS else 'moderate'}"]

    score += min(max(people_count, 0), 10) * 2
    if people_count > 0:
        reasons.append(f"people={people_count}")

    tags = _need_tags(needs)
    if "rescue" in tags:
        score += 18
        reasons.append("rescue/evacuation need")
    if "medical" in tags:
        score += 18
        reasons.append("medical need")
    if "water" in tags:
        score += 8
        reasons.append("safe-water need")
    if "shelter" in tags:
        score += 6
        reasons.append("shelter need")

    vulnerable = _normalize(vulnerable_groups)
    vulnerability_hits = sum(
        1 for keyword in ("child", "elder", "pregnant", "disabled", "injured", "infant") if keyword in vulnerable
    )
    if vulnerability_hits:
        score += min(vulnerability_hits * 6, 18)
        reasons.append("vulnerable people reported")

    score = min(score, 100)
    if score >= 80:
        band = "P1 - immediate human review"
    elif score >= 55:
        band = "P2 - urgent human review"
    elif score >= 30:
        band = "P3 - standard coordination"
    else:
        band = "P4 - monitor"
    return score, band, reasons


def _incident_similarity(existing: dict[str, Any], location: str, needs: str) -> float:
    location_score = _jaccard(_tokens(existing.get("location", "")), _tokens(location))
    need_score = _jaccard(set(existing.get("need_tags", [])), _need_tags(needs))
    return round((0.68 * location_score) + (0.32 * need_score), 3)


def _redact(value: str) -> str:
    value = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[redacted-email]", value)
    value = re.sub(r"(?<!\d)(?:\+?\d[\d\s().-]{7,}\d)(?!\d)", "[redacted-phone]", value)
    return value


def _coarsen_location(location: str) -> str:
    cleaned = re.sub(r"\b(?:house|home|unit|room|no\.?)[\s:#-]*\d+[A-Za-z]?\b", "", location, flags=re.I)
    cleaned = re.sub(r"\b\d{1,4}[A-Za-z]?\b", "", cleaned)
    cleaned = re.sub(r"\s+,", ",", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ,-\t")
    return cleaned or "location withheld"


def report_incident(
    location: str,
    needs: str,
    people_count: int = 1,
    severity: str = "moderate",
    vulnerable_groups: str = "",
    reporter_contact: str = "",
    notes: str = "",
) -> str:
    """Register a community incident, calculate transparent triage priority, and detect likely duplicate reports."""
    state = _get_state()
    duplicate_id = None
    duplicate_score = 0.0
    for incident_id, incident in state["incidents"].items():
        if incident.get("status") not in ACTIVE_STATUSES:
            continue
        score = _incident_similarity(incident, location, needs)
        if score > duplicate_score:
            duplicate_id, duplicate_score = incident_id, score

    priority_score, priority_band, reasons = _priority_score(people_count, severity, needs, vulnerable_groups)
    if duplicate_id and duplicate_score >= 0.72:
        incident = state["incidents"][duplicate_id]
        incident["report_count"] += 1
        incident["people_count"] = max(incident["people_count"], max(people_count, 0))
        incident["needs"] = sorted(set(incident["needs"]) | _need_tags(needs))
        incident["need_tags"] = incident["needs"]
        if priority_score > incident["priority_score"]:
            incident["priority_score"] = priority_score
            incident["priority_band"] = priority_band
            incident["priority_reasons"] = reasons
        incident["updated_at"] = _now()
        incident.setdefault("duplicate_reports", []).append(
            {"at": _now(), "similarity": duplicate_score, "notes": _redact(notes)}
        )
        state["timeline"].append({"at": _now(), "event": "duplicate_report", "incident_id": duplicate_id})
        _save_state(state)
        _remember_session(SESSION_LAST_INCIDENT_KEY, duplicate_id)
        _remember_session(SESSION_LAST_AREA_KEY, incident["location"])
        return json.dumps(
            {
                "result": "merged_with_likely_duplicate",
                "duplicate_of": duplicate_id,
                "similarity": duplicate_score,
                "incident": incident,
                "safety_note": "Priority is decision support only; a human coordinator must review dispatch decisions.",
            },
            indent=2,
        )

    incident_id = f"INC-{uuid4().hex[:8].upper()}"
    incident = {
        "incident_id": incident_id,
        "location": location.strip(),
        "needs": sorted(_need_tags(needs)),
        "need_tags": sorted(_need_tags(needs)),
        "people_count": max(people_count, 0),
        "severity": _normalize(severity),
        "vulnerable_groups": vulnerable_groups.strip(),
        "reporter_contact": reporter_contact.strip(),
        "notes": notes.strip(),
        "priority_score": priority_score,
        "priority_band": priority_band,
        "priority_reasons": reasons,
        "status": "open",
        "verified": False,
        "verification_events": [],
        "report_count": 1,
        "matched_resources": [],
        "created_at": _now(),
        "updated_at": _now(),
    }
    state["incidents"][incident_id] = incident
    state["timeline"].append({"at": _now(), "event": "incident_reported", "incident_id": incident_id})
    _save_state(state)
    _remember_session(SESSION_LAST_INCIDENT_KEY, incident_id)
    _remember_session(SESSION_LAST_AREA_KEY, incident["location"])
    return json.dumps(
        {
            "result": "created",
            "incident": incident,
            "safety_note": "Priority is decision support only; a human coordinator must review dispatch decisions.",
        },
        indent=2,
    )


def verify_incident(
    incident_id: str, verification_type: str, evidence_summary: str, verifier: str = "community"
) -> str:
    """Record a verification event without exposing evidence or reporter contact in public summaries."""
    state = _get_state()
    incident = state["incidents"].get(incident_id)
    if incident is None:
        return json.dumps({"error": "incident_not_found", "incident_id": incident_id})

    event = {
        "at": _now(),
        "verification_type": _normalize(verification_type),
        "evidence_summary": _redact(evidence_summary.strip()),
        "verifier": _redact(verifier.strip()),
    }
    incident["verification_events"].append(event)
    incident["verified"] = True
    if incident["status"] == "open":
        incident["status"] = "verified"
    incident["updated_at"] = _now()
    state["timeline"].append({"at": _now(), "event": "incident_verified", "incident_id": incident_id})
    _save_state(state)
    return json.dumps({"result": "verified", "incident": incident}, indent=2)


def get_incident(incident_id: str = "") -> str:
    """Return a private incident record; omitted ID reuses the active Agent Kernel session incident."""
    resolved_id = _resolve_incident_id(incident_id)
    incident = _get_state()["incidents"].get(resolved_id)
    if incident is None:
        return json.dumps({"error": "incident_not_found", "incident_id": resolved_id})
    _remember_session(SESSION_LAST_INCIDENT_KEY, resolved_id)
    _remember_session(SESSION_LAST_AREA_KEY, incident["location"])
    return json.dumps(incident, indent=2)


def public_incident_brief(incident_id: str = "") -> str:
    """Return a privacy-minimised brief; omitted ID reuses the active Agent Kernel session incident."""
    resolved_id = _resolve_incident_id(incident_id)
    incident = _get_state()["incidents"].get(resolved_id)
    if incident is None:
        return json.dumps({"error": "incident_not_found", "incident_id": resolved_id})
    _remember_session(SESSION_LAST_INCIDENT_KEY, resolved_id)
    brief = {
        "incident_id": incident["incident_id"],
        "area": _coarsen_location(incident["location"]),
        "needs": incident["needs"],
        "people_count": incident["people_count"],
        "priority_band": incident["priority_band"],
        "status": incident["status"],
        "verified": incident["verified"],
        "report_count": incident["report_count"],
        "note": "Private contact details and fine-grained location information are intentionally omitted.",
    }
    return json.dumps(brief, indent=2)


def register_resource(
    provider_name: str,
    resource_type: str,
    quantity: int,
    location: str,
    availability_minutes: int = 0,
    contact: str = "",
    notes: str = "",
) -> str:
    """Register a volunteer or organisation resource offer for later human-approved matching."""
    state = _get_state()
    resource_id = f"RES-{uuid4().hex[:8].upper()}"
    resource = {
        "resource_id": resource_id,
        "provider_name": provider_name.strip(),
        "resource_type": _normalize(resource_type),
        "quantity": max(quantity, 0),
        "location": location.strip(),
        "availability_minutes": max(availability_minutes, 0),
        "contact": contact.strip(),
        "notes": notes.strip(),
        "status": "available",
        "created_at": _now(),
    }
    state["resources"][resource_id] = resource
    state["timeline"].append({"at": _now(), "event": "resource_registered", "resource_id": resource_id})
    _save_state(state)
    _remember_session(SESSION_LAST_RESOURCE_KEY, resource_id)
    _remember_session(SESSION_LAST_AREA_KEY, resource["location"])
    return json.dumps({"result": "registered", "resource": resource}, indent=2)


def _client_request_id(value: str) -> str:
    """Normalize a caller-generated idempotency key without retaining user content."""
    cleaned = re.sub(r"[^A-Za-z0-9_.:-]", "", (value or "").strip())[:96]
    return cleaned or f"field-{uuid4().hex}"


def _field_incident_view(incident: dict[str, Any] | None) -> dict[str, Any] | None:
    if incident is None:
        return None
    return {
        "incident_id": incident.get("incident_id", ""),
        "priority_score": incident.get("priority_score", 0),
        "priority_band": incident.get("priority_band", ""),
        "status": incident.get("status", ""),
        "verified": bool(incident.get("verified")),
        "report_count": incident.get("report_count", 1),
    }


def _field_resource_view(resource: dict[str, Any] | None) -> dict[str, Any] | None:
    if resource is None:
        return None
    return {
        "resource_id": resource.get("resource_id", ""),
        "resource_type": resource.get("resource_type", ""),
        "quantity": resource.get("quantity", 0),
        "status": resource.get("status", ""),
    }


def submit_field_incident(
    client_request_id: str,
    location: str,
    needs: str,
    people_count: int = 1,
    severity: str = "moderate",
    vulnerable_groups: str = "",
    reporter_contact: str = "",
    notes: str = "",
) -> str:
    """Idempotently ingest an incident from an offline-capable field client."""
    request_id = _client_request_id(client_request_id)
    with _LEDGER_LOCK:
        state = _get_state()
        for event in state["timeline"]:
            if event.get("event") == "field_incident_received" and event.get("client_request_id") == request_id:
                incident = state["incidents"].get(event.get("incident_id", ""))
                return json.dumps(
                    {
                        "result": "idempotent_replay",
                        "client_request_id": request_id,
                        "incident": _field_incident_view(incident),
                        "original_result": event.get("original_result", "created"),
                    },
                    indent=2,
                )

        payload = json.loads(
            report_incident(
                location,
                needs,
                people_count=people_count,
                severity=severity,
                vulnerable_groups=vulnerable_groups,
                reporter_contact=reporter_contact,
                notes=notes,
            )
        )
        incident_id = payload.get("incident", {}).get("incident_id") or payload.get("duplicate_of", "")
        state = _get_state()
        state["timeline"].append(
            {
                "at": _now(),
                "event": "field_incident_received",
                "client_request_id": request_id,
                "incident_id": incident_id,
                "original_result": payload.get("result", "created"),
            }
        )
        _save_state(state)
        payload["client_request_id"] = request_id
        payload["incident"] = _field_incident_view(payload.get("incident"))
        return json.dumps(payload, indent=2)


def submit_field_resource(
    client_request_id: str,
    provider_name: str,
    resource_type: str,
    quantity: int,
    location: str,
    availability_minutes: int = 0,
    contact: str = "",
    notes: str = "",
) -> str:
    """Idempotently ingest a resource offer from an offline-capable field client."""
    request_id = _client_request_id(client_request_id)
    with _LEDGER_LOCK:
        state = _get_state()
        for event in state["timeline"]:
            if event.get("event") == "field_resource_received" and event.get("client_request_id") == request_id:
                resource = state["resources"].get(event.get("resource_id", ""))
                return json.dumps(
                    {
                        "result": "idempotent_replay",
                        "client_request_id": request_id,
                        "resource": _field_resource_view(resource),
                    },
                    indent=2,
                )

        payload = json.loads(
            register_resource(
                provider_name,
                resource_type,
                quantity,
                location,
                availability_minutes=availability_minutes,
                contact=contact,
                notes=notes,
            )
        )
        resource_id = payload.get("resource", {}).get("resource_id", "")
        state = _get_state()
        state["timeline"].append(
            {
                "at": _now(),
                "event": "field_resource_received",
                "client_request_id": request_id,
                "resource_id": resource_id,
            }
        )
        _save_state(state)
        payload["client_request_id"] = request_id
        payload["resource"] = _field_resource_view(payload.get("resource"))
        return json.dumps(payload, indent=2)


def list_available_resources(resource_type: str = "") -> str:
    """List available resources, optionally filtered by a resource type or need keyword."""
    state = _get_state()
    needle = _normalize(resource_type)
    resources = []
    for resource in state["resources"].values():
        if resource["status"] != "available":
            continue
        if (
            needle
            and needle not in resource["resource_type"]
            and needle not in RESOURCE_ALIASES.get(resource["resource_type"], set())
        ):
            continue
        public_resource = {key: value for key, value in resource.items() if key != "contact"}
        resources.append(public_resource)
    return json.dumps({"count": len(resources), "resources": resources}, indent=2)


def _resource_tags(resource_type: str) -> set[str]:
    normalized = _normalize(resource_type)
    tags = set(RESOURCE_ALIASES.get(normalized, set()))
    tags.update(_need_tags(normalized))
    return tags


def match_resources(incident_id: str = "", limit: int = 3) -> str:
    """Rank resources for an incident; omitted ID reuses the active Agent Kernel session incident."""
    state = _get_state()
    resolved_id = _resolve_incident_id(incident_id)
    incident = state["incidents"].get(resolved_id)
    if incident is None:
        return json.dumps({"error": "incident_not_found", "incident_id": resolved_id})
    _remember_session(SESSION_LAST_INCIDENT_KEY, resolved_id)

    incident_tags = set(incident["need_tags"])
    incident_location = _tokens(incident["location"])
    matches: list[dict[str, Any]] = []
    for resource in state["resources"].values():
        if resource["status"] != "available" or resource["quantity"] <= 0:
            continue
        resource_tags = _resource_tags(resource["resource_type"])
        need_overlap = incident_tags & resource_tags
        if not need_overlap:
            continue
        location_score = _jaccard(incident_location, _tokens(resource["location"]))
        score = 60 + int(location_score * 25)
        if resource["availability_minutes"] <= 30:
            score += 10
        elif resource["availability_minutes"] <= 120:
            score += 5
        score += min(resource["quantity"], 5)
        matches.append(
            {
                "resource_id": resource["resource_id"],
                "resource_type": resource["resource_type"],
                "provider_name": resource["provider_name"],
                "quantity": resource["quantity"],
                "location": resource["location"],
                "availability_minutes": resource["availability_minutes"],
                "matched_needs": sorted(need_overlap),
                "match_score": min(score, 100),
            }
        )

    matches.sort(key=lambda item: (-item["match_score"], item["availability_minutes"], item["resource_id"]))
    return json.dumps(
        {
            "incident_id": resolved_id,
            "priority_band": incident["priority_band"],
            "proposed_matches": matches[: max(1, min(limit, 10))],
            "dispatch_policy": "Proposal only. A human coordinator must explicitly confirm any match.",
        },
        indent=2,
    )


def network_allocation_plan(max_assignments: int = 12) -> str:
    """Create a dry-run, network-wide allocation plan without reserving any resource.

    Unlike ``match_resources`` (one incident at a time), this planner considers every active
    incident together so a scarce boat/ambulance cannot be proposed to several incidents at once.
    It rewards high-priority and verified incidents, while also giving a first-coverage bonus so
    lower-priority incidents are not silently starved. Human confirmation is still mandatory.
    """
    state = _get_state()
    incidents = [incident for incident in state["incidents"].values() if incident.get("status") in ACTIVE_STATUSES]
    resources = [
        resource
        for resource in state["resources"].values()
        if resource.get("status") == "available" and int(resource.get("quantity", 0)) > 0
    ]
    max_assignments = max(1, min(int(max_assignments), 30))
    used_resources: set[str] = set()
    covered: dict[str, set[str]] = {incident["incident_id"]: set() for incident in incidents}
    assignments: list[dict[str, Any]] = []

    while len(assignments) < max_assignments:
        best: tuple[float, dict[str, Any]] | None = None
        for incident in incidents:
            incident_id = incident["incident_id"]
            incident_tags = set(incident.get("need_tags", []))
            remaining = incident_tags - covered[incident_id]
            if not remaining:
                continue
            for resource in resources:
                resource_id = resource["resource_id"]
                if resource_id in used_resources:
                    continue
                overlap = remaining & _resource_tags(resource["resource_type"])
                if not overlap:
                    continue
                location_score = _jaccard(_tokens(incident["location"]), _tokens(resource["location"]))
                match_score = 60 + int(location_score * 25)
                if resource["availability_minutes"] <= 30:
                    match_score += 10
                elif resource["availability_minutes"] <= 120:
                    match_score += 5
                match_score += min(int(resource["quantity"]), 5)
                match_score = min(match_score, 100)

                priority = int(incident.get("priority_score", 0))
                verified_bonus = 5 if incident.get("verified") else 0
                first_coverage_bonus = 6 if not covered[incident_id] else 0
                corroboration_bonus = min(max(int(incident.get("report_count", 1)) - 1, 0) * 2, 5)
                allocation_score = round(
                    (priority * 0.55)
                    + (match_score * 0.34)
                    + verified_bonus
                    + first_coverage_bonus
                    + corroboration_bonus,
                    1,
                )
                candidate = {
                    "incident_id": incident_id,
                    "priority_band": incident["priority_band"],
                    "verified": bool(incident.get("verified")),
                    "resource_id": resource_id,
                    "resource_type": resource["resource_type"],
                    "provider_name": resource["provider_name"],
                    "matched_needs": sorted(overlap),
                    "availability_minutes": resource["availability_minutes"],
                    "match_score": match_score,
                    "allocation_score": allocation_score,
                    "rationale": [
                        f"incident_priority={priority}",
                        f"resource_match={match_score}",
                        "verified_report" if incident.get("verified") else "verification_pending",
                        "first_resource_for_incident" if not covered[incident_id] else "additional_need_coverage",
                    ],
                }
                # Store a sortable scalar with tiny deterministic tie breakers.
                packed = allocation_score + (match_score / 10000) - (int(resource["availability_minutes"]) / 1_000_000)
                if best is None or packed > best[0]:
                    best = (packed, candidate)
        if best is None:
            break
        candidate = best[1]
        used_resources.add(candidate["resource_id"])
        covered[candidate["incident_id"]].update(candidate["matched_needs"])
        assignments.append(candidate)

    unserved = []
    for incident in sorted(incidents, key=lambda item: (-int(item.get("priority_score", 0)), item["incident_id"])):
        unmet = sorted(set(incident.get("need_tags", [])) - covered[incident["incident_id"]])
        if unmet:
            unserved.append(
                {
                    "incident_id": incident["incident_id"],
                    "priority_band": incident["priority_band"],
                    "verified": bool(incident.get("verified")),
                    "unmet_needs": unmet,
                }
            )

    return json.dumps(
        {
            "mode": "dry_run_network_allocation",
            "assignments": assignments,
            "unserved_or_partially_served": unserved,
            "resources_considered": len(resources),
            "active_incidents_considered": len(incidents),
            "policy": (
                "No resource is reserved by this plan. It avoids double-proposing a scarce resource, "
                "balances urgency with first-coverage fairness, and still requires explicit human confirmation."
            ),
        },
        indent=2,
    )


def command_center_snapshot() -> str:
    """Return a privacy-safe live board for the local judge command center."""
    state = _get_state()
    metrics = json.loads(operations_snapshot())
    incidents = []
    for incident in sorted(
        state["incidents"].values(), key=lambda item: (-int(item.get("priority_score", 0)), item["incident_id"])
    ):
        incidents.append(
            {
                "incident_id": incident["incident_id"],
                "location": _coarsen_location(incident.get("location", "")),
                "needs": incident.get("needs", []),
                "need_tags": incident.get("need_tags", []),
                "people_count": incident.get("people_count", 0),
                "priority_score": incident.get("priority_score", 0),
                "priority_band": incident.get("priority_band", ""),
                "priority_reasons": incident.get("priority_reasons", []),
                "status": incident.get("status", ""),
                "verified": bool(incident.get("verified")),
                "report_count": incident.get("report_count", 1),
                "matched_resources": incident.get("matched_resources", []),
                "updated_at": incident.get("updated_at", ""),
            }
        )
    resources = []
    for resource in state["resources"].values():
        resources.append(
            {key: value for key, value in resource.items() if key not in {"contact", "notes", "reviewed_by"}}
        )
    timeline = []
    for event in state["timeline"][-40:]:
        safe = {key: (_redact(value) if isinstance(value, str) else value) for key, value in event.items()}
        timeline.append(safe)
    return json.dumps(
        {"metrics": metrics, "incidents": incidents, "resources": resources, "timeline": timeline}, indent=2
    )


def confirm_match(incident_id: str, resource_id: str, reviewer: str) -> str:
    """Confirm a resource-to-incident match after an explicit human review decision."""
    state = _get_state()
    incident = state["incidents"].get(incident_id)
    resource = state["resources"].get(resource_id)
    if incident is None:
        return json.dumps({"error": "incident_not_found", "incident_id": incident_id})
    if resource is None:
        return json.dumps({"error": "resource_not_found", "resource_id": resource_id})
    if resource["status"] != "available":
        return json.dumps({"error": "resource_not_available", "resource_id": resource_id})
    if not reviewer.strip():
        return json.dumps({"error": "reviewer_required"})

    resource["status"] = "reserved"
    resource["reserved_for"] = incident_id
    resource["reviewed_by"] = _redact(reviewer.strip())
    match_event = {"resource_id": resource_id, "reviewer": _redact(reviewer.strip()), "at": _now()}
    incident["matched_resources"].append(match_event)
    if incident["status"] in {"open", "verified"}:
        incident["status"] = "matched"
    incident["updated_at"] = _now()
    state["timeline"].append(
        {"at": _now(), "event": "match_confirmed", "incident_id": incident_id, "resource_id": resource_id}
    )
    _save_state(state)
    return json.dumps({"result": "match_confirmed", "incident_id": incident_id, "resource_id": resource_id}, indent=2)


def update_incident_status(incident_id: str, status: str, note: str = "") -> str:
    """Update an incident lifecycle status and append a privacy-redacted coordination note."""
    normalized_status = _normalize(status)
    if normalized_status not in ALLOWED_STATUSES:
        return json.dumps({"error": "invalid_status", "allowed": sorted(ALLOWED_STATUSES)})
    state = _get_state()
    incident = state["incidents"].get(incident_id)
    if incident is None:
        return json.dumps({"error": "incident_not_found", "incident_id": incident_id})
    incident["status"] = normalized_status
    incident["updated_at"] = _now()
    state["timeline"].append(
        {
            "at": _now(),
            "event": "status_updated",
            "incident_id": incident_id,
            "status": normalized_status,
            "note": _redact(note),
        }
    )
    _save_state(state)
    return json.dumps({"result": "status_updated", "incident_id": incident_id, "status": normalized_status}, indent=2)


def operations_snapshot() -> str:
    """Return privacy-safe operational metrics for coordinators and judges."""
    state = _get_state()
    incidents = list(state["incidents"].values())
    resources = list(state["resources"].values())
    snapshot = {
        "incidents_total": len(incidents),
        "incidents_active": sum(incident["status"] in ACTIVE_STATUSES for incident in incidents),
        "incidents_verified": sum(bool(incident["verified"]) for incident in incidents),
        "people_reported": sum(int(incident["people_count"]) for incident in incidents),
        "available_resources": sum(resource["status"] == "available" for resource in resources),
        "confirmed_matches": sum(len(incident["matched_resources"]) for incident in incidents),
        "duplicate_reports_merged": sum(max(int(incident["report_count"]) - 1, 0) for incident in incidents),
        "priority_counts": {},
    }
    for incident in incidents:
        band = incident["priority_band"].split(" - ", 1)[0]
        snapshot["priority_counts"][band] = snapshot["priority_counts"].get(band, 0) + 1
    return json.dumps(snapshot, indent=2)
