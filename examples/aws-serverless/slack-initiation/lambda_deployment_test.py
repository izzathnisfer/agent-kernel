"""
Live deployment smoke test for the slack-initiation serverless example.

Runs only against an already-deployed stack, using AK_TEST_ENDPOINT and
SLACK_SIGNING_SECRET — both supplied by the weekly integration test workflow
(see .github/integration-test-config.yaml and
.github/workflows/integration-test-weekly.yaml, which runs
.github/scripts/run_single_test.py's deploy -> test -> destroy cycle). Skipped
when those aren't set, so a plain local `uv run pytest` here still runs
lambda_test.py's local unit tests unaffected.

Unlike lambda_test.py (which calls handle_slack_events() directly, in-process,
against fakes), this test sends a real, signed HTTP request to the deployed
API Gateway endpoint — the only way to catch a broken deployment (e.g. a
missing/mismatched dependency causing Runtime.ImportModuleError at Lambda cold
start), since a local-only test imports the same already-working source tree
and can never see a packaging problem in the actual deployed artifact.
"""

import hashlib
import hmac
import json
import os
import time

import httpx
import pytest

AK_TEST_ENDPOINT = os.getenv("AK_TEST_ENDPOINT")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")

# AK_TEST_ENDPOINT is the agent_invoke_url Terraform output —
# {stage}/api/v1/chat. The api-gateway module mounts every custom
# gateway_endpoints path under the same {api_base_path}/{api_version} prefix,
# so the Slack webhook lives at {stage}/api/v1/slack/events: swap the trailing
# agent endpoint for the webhook path (main.tf pins agent_endpoint = "chat").
SLACK_EVENTS_URL = f"{AK_TEST_ENDPOINT.removesuffix('/chat')}/slack/events" if AK_TEST_ENDPOINT else None

pytestmark = pytest.mark.skipif(
    not AK_TEST_ENDPOINT or not SLACK_SIGNING_SECRET,
    reason="requires a deployed endpoint (AK_TEST_ENDPOINT) and SLACK_SIGNING_SECRET — set by the weekly integration test workflow",
)


def _sign(body: bytes) -> dict:
    """Signs a request body the way Slack does, using the same signing secret
    Terraform gave the deployed Lambda (deploy/main.tf's SLACK_SIGNING_SECRET
    environment variable, sourced from the same CI secret as this test)."""
    timestamp = str(int(time.time()))
    base = b"v0:" + timestamp.encode() + b":" + body
    signature = "v0=" + hmac.new(SLACK_SIGNING_SECRET.encode(), base, hashlib.sha256).hexdigest()
    return {
        "content-type": "application/json",
        "x-slack-request-timestamp": timestamp,
        "x-slack-signature": signature,
    }


def test_slack_events_endpoint_accepts_url_verification():
    """
    Round-trips Slack's url_verification handshake against the live endpoint.
    A 200 with the challenge echoed back proves the deployed Lambda actually
    imported and ran its code end to end (request URL, API Gateway routing,
    cold start, signature verification, JSON handling) — cheap, side-effect-free,
    and would have failed with a 500 for a "No module named
    agentkernel.core.initiation"-style packaging regression.
    """
    challenge = "ci-smoke-test-challenge"
    body = json.dumps({"type": "url_verification", "challenge": challenge}).encode()

    resp = httpx.post(SLACK_EVENTS_URL, content=body, headers=_sign(body), timeout=30.0)

    assert resp.status_code == 200
    assert resp.json()["challenge"] == challenge


def test_slack_events_endpoint_rejects_unsigned_request():
    """Confirms signature verification is actually active on the deployed Lambda."""
    body = json.dumps({"type": "url_verification", "challenge": "x"}).encode()

    resp = httpx.post(
        SLACK_EVENTS_URL,
        content=body,
        headers={"content-type": "application/json"},
        timeout=30.0,
    )

    assert resp.status_code == 401
